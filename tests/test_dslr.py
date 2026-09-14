import asyncio
import io
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import camera, dslr
from app.main import create_app
from app.models import Settings
from app.projects import Projects


DEVICE = 'gphoto2:usb:001,006'
DETECTED = ('Model                          Port\n'
            '----------------------------------------------------------\n'
            'Canon EOS 800D                 usb:001,006\n'
            'Canon EOS Rebel T7i            usb:002,011\n')


@pytest.fixture(autouse=True)
def usb_sysfs(tmp_path_factory):
    root = tmp_path_factory.mktemp('usb-sysfs')
    with patch('app.dslr.USB_SYSFS', root):
        yield root


def usb_camera(root, name='1-2', bus=1, address=6, serial='camera-one'):
    device = root / name
    device.mkdir(exist_ok=True)
    for key, value in {'busnum': bus, 'devnum': address, 'serial': serial,
                       'idVendor': '04a9', 'idProduct': '32c9'}.items():
        (device / key).write_text(str(value) + '\n')
    return device


@pytest.fixture
def jpeg():
    output = io.BytesIO()
    exif = Image.Exif()
    exif[271] = 'Canon'
    exif[274] = 6  # Portrait camera orientation, stored as a landscape JPEG.
    Image.new('RGB', (1200, 800), '#507747').save(output, 'JPEG', exif=exif)
    return output.getvalue()


@pytest.fixture
def gphoto(jpeg):
    def run(command, **kwargs):
        assert command[0] == 'gphoto2'
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['env']['LC_ALL'] == 'C'
        if '--auto-detect' in command:
            assert command == ['gphoto2', '--auto-detect']
            return SimpleNamespace(returncode=0, stdout=DETECTED, stderr='')
        assert command[command.index('--port') + 1] == 'usb:001,006'
        assert command[command.index('--camera') + 1] == 'Canon EOS 800D'
        assert command[command.index('--filename') + 1] == 'capture-%n.%C'
        assert '--capture-image-and-download' in command and '--keep' in command
        assert not any(arg.startswith('--set-config') for arg in command)
        assert kwargs['timeout'] == 90
        (Path(kwargs['cwd']) / 'capture-1.JPG').write_bytes(jpeg)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    with patch('app.dslr.subprocess.run', side_effect=run) as process:
        yield process


def test_discovery_and_capabilities_do_not_take_photos(gphoto):
    with patch('app.camera.device_path') as paths:
        paths.return_value.glob.return_value = []
        inventory = camera.list_devices()
    assert [c['id'] for c in inventory] == [DEVICE, 'gphoto2:usb:002,011']
    assert inventory[0]['name'] == 'Canon EOS 800D'
    assert inventory[0]['backend'] == 'gphoto2'
    result = camera.capabilities(DEVICE)
    assert result['resolution_scope'] == 'export'
    assert result['recommended']['width'] == 1920
    assert camera.focus_capabilities(DEVICE)['autofocus'] is False
    assert gphoto.call_count == 1  # Capability endpoints do not open the camera.


def test_capture_preserves_full_resolution_exif_and_cleans_downloads(tmp_path, gphoto, jpeg):
    # Even percent signs in a user's data path cannot change the filename pattern.
    directory = tmp_path / 'study%Y'
    directory.mkdir()
    target = directory / 'photo.jpg'
    with patch('app.camera.probe_device') as probe:
        camera.capture(target, Settings(camera_device=DEVICE, width=640, height=480), False)
        probe.assert_not_called()
    assert target.read_bytes() == jpeg
    with Image.open(target) as image:
        assert image.size == (1200, 800) and image.getexif()[271] == 'Canon'
    assert list(directory.iterdir()) == [target]


@pytest.mark.parametrize('download', ['raw', 'raw+jpeg', 'corrupt', 'missing', 'multiple'])
def test_download_validation_and_cleanup(tmp_path, jpeg, download):
    def run(command, **kwargs):
        if '--auto-detect' in command:
            return SimpleNamespace(returncode=0, stdout=DETECTED, stderr='')
        directory = Path(kwargs['cwd'])
        if download in {'raw', 'raw+jpeg'}:
            (directory / 'capture-1.CR2').write_bytes(b'raw data')
        if download in {'raw+jpeg', 'multiple'}:
            (directory / 'capture-2.JPG').write_bytes(jpeg)
        if download == 'multiple':
            (directory / 'capture-3.JPG').write_bytes(jpeg)
        if download == 'corrupt':
            (directory / 'capture-1.JPG').write_bytes(b'not an image')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    target = tmp_path / 'photo.jpg'
    with patch('app.dslr.subprocess.run', side_effect=run):
        if download == 'raw+jpeg':
            camera.capture(target, Settings(camera_device=DEVICE), False)
            assert target.read_bytes() == jpeg
            target.unlink()
        else:
            with pytest.raises(RuntimeError, match='JPEG'):
                camera.capture(target, Settings(camera_device=DEVICE), False)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure, message', [
    (FileNotFoundError(), 'requires gphoto2'),
    (subprocess.TimeoutExpired('gphoto2', 90), 'timed out'),
    (PermissionError('denied'), 'Cannot run gphoto2'),
    (SimpleNamespace(returncode=1, stdout='', stderr='Could not claim USB device'), 'USB permissions'),
    (SimpleNamespace(returncode=0, stdout='', stderr='\n*** Error ***\nCanon EOS Capture failed to release: Perhaps no focus?\nERROR: Could not capture image.\n'), 'Perhaps no focus'),
])
def test_cli_failures_are_actionable_and_do_not_publish(tmp_path, failure, message):
    with patch('app.dslr.discover', return_value=[{'id': DEVICE, 'name': 'Canon EOS 800D'}]), \
         patch('app.dslr.subprocess.run', side_effect=failure if isinstance(failure, Exception) else None,
               return_value=failure):
        with pytest.raises(RuntimeError, match=message):
            camera.capture(tmp_path / 'photo.jpg', Settings(camera_device=DEVICE), False)
    assert not list(tmp_path.iterdir())


def test_disconnected_selection_never_captures_another_camera(tmp_path, gphoto):
    with pytest.raises(RuntimeError, match='reselect'):
        camera.capture(tmp_path / 'photo.jpg', Settings(camera_device='gphoto2:usb:001,007'), False)
    assert gphoto.call_count == 1
    assert not list(tmp_path.iterdir())


def test_serial_identity_survives_address_change_and_rejects_duplicates(usb_sysfs):
    node = usb_camera(usb_sysfs)
    identity = dslr.stable_device(DEVICE)
    assert identity.startswith('gphoto2:serial:')
    (node / 'devnum').write_text('42')
    assert dslr.stable_device('gphoto2:usb:001,042') == identity
    assert dslr.stable_device(DEVICE) == DEVICE
    usb_camera(usb_sysfs, name='1-3', address=43)
    assert dslr.usb_serial_ids() == {}  # Never guess between duplicate serials.
    (node / 'serial').write_text('')
    assert dslr.stable_device('gphoto2:usb:001,043') == identity


@pytest.mark.parametrize('serial', ['', '000000'])
def test_unusable_serial_keeps_explicit_address(usb_sysfs, serial):
    usb_camera(usb_sysfs, serial=serial)
    assert dslr.stable_device(DEVICE) == DEVICE


def test_serial_capture_reconnects_without_using_camera_at_old_address(tmp_path, usb_sysfs, jpeg):
    node = usb_camera(usb_sysfs)
    identity = dslr.stable_device(DEVICE)
    settings = Settings(camera_device=identity)
    (node / 'devnum').write_text('42')
    usb_camera(usb_sysfs, name='1-3', address=6, serial='different-camera')
    def run(command, **kwargs):
        if '--auto-detect' in command:
            return SimpleNamespace(returncode=0, stdout=DETECTED + 'Canon EOS 800D    usb:001,042\n', stderr='')
        assert command[command.index('--port') + 1] == 'usb:001,042'
        (Path(kwargs['cwd']) / 'capture-1.JPG').write_bytes(jpeg)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    with patch('app.dslr.subprocess.run', side_effect=run):
        camera.capture(tmp_path / 'photo.jpg', settings, False)
        assert (tmp_path / 'photo.jpg').read_bytes() == jpeg
        (node / 'serial').unlink()
        with pytest.raises(RuntimeError, match='Reconnect the same camera'):
            camera.capture(tmp_path / 'missing.jpg', settings, False)
    assert not (tmp_path / 'missing.jpg').exists()


def test_serial_settings_persist_and_scheduler_recovers_after_reconnect(tmp_path, usb_sysfs, jpeg):
    node = usb_camera(usb_sysfs)
    identity = dslr.stable_device(DEVICE)
    port = 'usb:001,006'
    def run(command, **kwargs):
        if '--auto-detect' in command:
            return SimpleNamespace(returncode=0, stdout=f'Canon EOS 800D    {port}\n' if port else '', stderr='')
        assert command[command.index('--port') + 1] == port
        (Path(kwargs['cwd']) / 'capture-1.JPG').write_bytes(jpeg)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    async def scenario():
        nonlocal port
        manager = Projects(tmp_path / 'recording', False)
        rec = manager.get('default')
        await rec.save_settings(Settings(camera_device=DEVICE))
        assert rec.settings().camera_device == identity
        await rec.set_running(True)
        await rec.tick()
        port = ''
        state = rec.store.get('runtime')
        state['next_capture_at'] = time.time() - 1
        rec.store.put('runtime', state)
        with pytest.raises(RuntimeError, match='Reconnect the same camera'):
            await rec.tick()
        assert rec.status()['runtime']['running']
        rec = Projects(tmp_path / 'recording', False).get('default')
        assert rec.settings().camera_device == identity
        port = 'usb:002,042'
        (node / 'busnum').write_text('2')
        (node / 'devnum').write_text('42')
        state = rec.store.get('runtime')
        state['next_capture_at'] = time.time() - 1
        rec.store.put('runtime', state)
        await rec.tick()
        assert rec.status()['frames']['count'] == 2
        assert rec.status()['runtime']['last_error'] is None
    with patch('app.dslr.subprocess.run', side_effect=run):
        asyncio.run(scenario())


def test_api_normalizes_serial_on_save_but_preview_does_not_change_settings(tmp_path, usb_sysfs, gphoto):
    usb_camera(usb_sysfs)
    identity = dslr.stable_device(DEVICE)
    settings = Settings(camera_device=DEVICE).model_dump()
    with TestClient(create_app(tmp_path / 'api', demo=False)) as client:
        assert client.post('/api/preview', json={'settings': settings}).status_code == 200
        assert client.get('/api/status').json()['settings']['camera_device'] == '/dev/video0'
        assert client.put('/api/settings', json=settings).json()['camera_device'] == identity
        assert client.get('/api/status').json()['settings']['camera_device'] == identity
        assert any(c['id'] == identity and DEVICE in c['aliases'] for c in client.get('/api/cameras').json()['cameras'])


@pytest.mark.parametrize('success', [True, False])
def test_legacy_settings_upgrade_only_after_successful_saved_capture(tmp_path, usb_sysfs, gphoto, success):
    usb_camera(usb_sysfs)
    identity = dslr.stable_device(DEVICE)
    app = create_app(tmp_path / 'legacy', demo=False)
    app.state.recorder.store.put('settings', Settings(camera_device=DEVICE).model_dump())
    with TestClient(app) as client:
        assert client.post('/api/preview', json={}).status_code == 200
        assert client.get('/api/status').json()['settings']['camera_device'] == DEVICE
        if success:
            assert client.post('/api/capture', json={}).status_code == 200
        else:
            with patch('app.dslr.discover', return_value=[]):
                assert client.post('/api/capture', json={}).status_code == 503
        assert client.get('/api/status').json()['settings']['camera_device'] == (identity if success else DEVICE)


def test_missing_gphoto_keeps_webcam_discovery_working(tmp_path):
    with patch('app.dslr.subprocess.run', side_effect=FileNotFoundError()), \
         patch('app.camera.device_path', return_value=tmp_path):
        assert camera.list_devices() == []


def test_api_preview_capture_validation_and_restart(tmp_path, gphoto, jpeg):
    settings = Settings(camera_device=DEVICE, width=640, height=480).model_dump()
    with TestClient(create_app(tmp_path, demo=False)) as client:
        assert client.get('/api/cameras/capabilities', params={'device': DEVICE}).json()['resolution_scope'] == 'export'
        assert client.get('/api/cameras/focus', params={'device': DEVICE}).json()['backend'] == 'gphoto2'
        assert client.post('/api/preview', json={'settings': settings}).content == jpeg
        assert client.get('/api/status').json()['settings']['camera_device'] == '/dev/video0'
        assert client.get('/api/frames').json() == []
        assert client.put('/api/settings', json=settings).status_code == 200
        frame = client.post('/api/capture', json={}).json()
        assert client.get(f'/media/frames/{frame["id"]}.jpg').content == jpeg
        thumbnail = client.get(f'/media/thumbs/{frame["id"]}.jpg').content
        with Image.open(io.BytesIO(thumbnail)) as image:
            assert image.size == (213, 320)  # Respect orientation in the gallery.
        for bad in ['gphoto2:auto', 'gphoto2:usb:001,006;id', 'gphoto2:usb:1,6', 'gphoto2:../etc/passwd']:
            invalid = {**settings, 'camera_device': bad}
            assert client.put('/api/settings', json=invalid).status_code == 422
            assert client.post('/api/preview', json={'settings': invalid}).status_code == 422
            assert client.get('/api/cameras/capabilities', params={'device': bad}).status_code == 422
    with TestClient(create_app(tmp_path, demo=False)) as client:
        assert client.get('/api/status').json()['settings'] == settings
        assert len(client.get('/api/frames').json()) == 1


def test_dslr_schedule_serialization_and_retry(tmp_path, jpeg):
    active, maximum = 0, 0
    guard = threading.Lock()
    def run(command, **kwargs):
        nonlocal active, maximum
        if '--auto-detect' in command:
            return SimpleNamespace(returncode=0, stdout=DETECTED, stderr='')
        with guard:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(.02)
            (Path(kwargs['cwd']) / 'capture-1.JPG').write_bytes(jpeg)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        finally:
            with guard:
                active -= 1
    async def scenario():
        manager = Projects(tmp_path, False)
        second = await manager.create(Settings(camera_device=DEVICE))
        recs = [manager.get('default'), manager.get(second['id'])]
        for rec in recs:
            await rec.save_settings(Settings(camera_device=DEVICE))
            await rec.set_running(True)
        with patch('app.dslr.subprocess.run', side_effect=run):
            await asyncio.gather(*(rec.tick() for rec in recs))
        assert maximum == 1
        assert [rec.status()['frames']['count'] for rec in recs] == [1, 1]
        rec = recs[0]
        state = rec.store.get('runtime')
        state['next_capture_at'] = time.time() - 1
        rec.store.put('runtime', state)
        with patch('app.dslr.discover', return_value=[]), pytest.raises(RuntimeError, match='unavailable'):
            await rec.tick()
        state = rec.status()['runtime']
        assert state['running'] and 0 < state['next_capture_at'] - time.time() <= 60
        assert rec.status()['frames']['count'] == 1
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_full_resolution_dslr_photo_exports_at_selected_video_size(tmp_path, gphoto):
    async def scenario():
        manager = Projects(tmp_path, False)
        rec = manager.get('default')
        await rec.save_settings(Settings(camera_device=DEVICE, width=640, height=480))
        await rec.take_photo()
        job = await rec.create_export()
        await rec.export_task
        assert rec.store.rows('SELECT status FROM exports')[0]['status'] == 'complete'
        return job
    job = asyncio.run(scenario())
    # subprocess.run is mocked for gPhoto2; use Popen directly for real FFprobe.
    with subprocess.Popen(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json',
                           str(tmp_path / 'exports' / f'{job["id"]}.mp4')], stdout=subprocess.PIPE) as process:
        output, _ = process.communicate()
        assert process.returncode == 0
    stream = json.loads(output)['streams'][0]
    assert (stream['width'], stream['height'], int(stream['nb_frames'])) == (640, 480, 1)
