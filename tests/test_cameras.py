import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import camera
from app.main import create_app


@pytest.fixture(autouse=True)
def isolate_webcam_tests():
    # Connected DSLR hardware must not change these webcam-only inventories.
    with patch('app.camera.dslr.list_devices', return_value=[]):
        yield


def test_camera_selection_persists_and_test_preview_does_not_change_it(tmp_path):
    inputs = []

    def ffmpeg(command, **kwargs):
        inputs.append(command[command.index('-i') + 1])
        Image.new('RGB', (640, 480)).save(command[-1], 'JPEG')
        return SimpleNamespace(returncode=0)

    with patch('app.camera.probe_device', return_value={'name': 'USB camera', 'capture': True}), \
         patch('app.camera.subprocess.run', side_effect=ffmpeg):
        with TestClient(create_app(tmp_path, demo=False)) as client:
            settings = client.get('/api/status').json()['settings']
            settings['camera_device'] = '/dev/video2'
            settings['autofocus'] = False
            assert client.put('/api/settings', json=settings).status_code == 200
            candidate = {**settings, 'camera_device': '/dev/video4'}
            assert client.post('/api/preview', json={'settings': candidate}).status_code == 200
            assert client.get('/api/status').json()['settings']['camera_device'] == '/dev/video2'
            assert client.get('/api/frames').json() == []
            assert client.post('/api/preview', json={}).status_code == 200
            assert client.post('/api/capture', json={}).status_code == 200
        with TestClient(create_app(tmp_path, demo=False)) as client:
            assert client.get('/api/status').json()['settings']['camera_device'] == '/dev/video2'
    assert inputs == ['/dev/video4', '/dev/video2', '/dev/video2']


def test_old_settings_get_default_camera_and_invalid_paths_are_rejected(tmp_path):
    app = create_app(tmp_path, demo=True)
    old = app.state.recorder.store.get('settings')
    old.pop('camera_device')
    app.state.recorder.store.put('settings', old)
    with TestClient(app) as client:
        settings = client.get('/api/status').json()['settings']
        assert settings['camera_device'] == '/dev/video0'
        for path in ('/etc/passwd', 'http://camera', '/dev/video0/../../etc/passwd', '/dev/video0;whoami'):
            assert client.put('/api/settings', json={**settings, 'camera_device': path}).status_code == 422
            assert client.post('/api/preview', json={'settings': {**settings, 'camera_device': path}}).status_code == 422
        assert client.get('/api/cameras').json()['cameras'][0]['name'] == 'Demo camera (synthetic)'


def test_missing_selected_camera_fails_without_falling_back(tmp_path):
    with TestClient(create_app(tmp_path, demo=False)) as client:
        with patch('app.camera.probe_device', side_effect=FileNotFoundError('Disconnected')), \
             patch('app.camera.subprocess.run') as process:
            response = client.post('/api/capture', json={})
            assert response.status_code == 503
            assert 'Select an available webcam' in response.json()['detail']
            process.assert_not_called()
            assert client.get('/api/frames').json() == []


def test_probe_uses_device_capabilities_instead_of_whole_camera_capabilities():
    def ioctl(fd, request, buffer, mutate):
        assert request == 0x80685600
        buffer[:] = struct.pack('16s32s32s6I', b'uvcvideo', b'USB Camera', b'usb-1', 0,
                                0x80000001, 0x00800000, 0, 0, 0)  # Metadata node

    node = MagicMock(spec=Path)
    node.stat.return_value = SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=os.makedev(81, 1))
    with patch('app.camera.os.open', return_value=42), patch('app.camera.os.close') as close, \
         patch('app.camera.fcntl.ioctl', side_effect=ioctl):
        assert camera.probe_device(node) == {'name': 'USB Camera', 'capture': False}
        close.assert_called_once_with(42)


def test_discovery_deduplicates_stable_ids_and_omits_metadata():
    def device(path, minor):
        node = MagicMock(spec=Path)
        node.__str__.return_value = path
        node.name = path.rsplit('/', 1)[-1]
        node.stat.return_value = SimpleNamespace(st_rdev=os.makedev(81, minor))
        return node

    stable = device('/dev/v4l/by-id/usb-Camera-video-index0', 0)
    video = device('/dev/video0', 0)
    metadata = device('/dev/video1', 1)
    inaccessible = device('/dev/video2', 2)
    def probe(node):
        if node is inaccessible:
            raise PermissionError('Permission denied')
        return {'name': 'USB Camera', 'capture': node is not metadata}

    with patch('app.camera.device_path') as paths, patch('app.camera.probe_device', side_effect=probe):
        paths.return_value.glob.side_effect = lambda pattern: [stable] if pattern == '*' else [video, metadata, inaccessible]
        devices = camera.list_devices()
    assert len(devices) == 2
    assert devices[0]['id'] == str(stable)
    assert devices[0]['aliases'] == [str(stable), '/dev/video0']
    assert not devices[1]['available']
    assert 'Permission denied' in devices[1]['error']


def test_empty_discovery_and_api(tmp_path):
    with TestClient(create_app(tmp_path, demo=False)) as client, patch('app.camera.device_path') as paths:
        paths.return_value.glob.return_value = []
        assert client.get('/api/cameras').json() == {'demo': False, 'cameras': []}


def test_resolution_detection_selects_largest_compatible_format():
    import errno
    formats = [(b'YUYV', [(640,480), (1920,1080)]), (b'MJPG', [(1280,720), (3840,2160)]), (b'XXXX', [(8000,8000)])]
    def ioctl(fd, request, buffer, mutate):
        index = struct.unpack_from('I', buffer)[0]
        if request == 0xC0405602:
            if index >= len(formats):
                raise OSError(errno.EINVAL, 'End of formats')
            buffer[44:48] = formats[index][0]
        elif request == 0xC02C564A:
            sizes = next(sizes for fourcc,sizes in formats if fourcc == bytes(buffer[4:8]))
            if index >= len(sizes):
                raise OSError(errno.EINVAL, 'End of sizes')
            struct.pack_into('III', buffer, 8, 1, *sizes[index])
        else:
            raise AssertionError('Unexpected ioctl; detection must not set a mode or capture')
    with patch('app.camera.probe_device', return_value={'capture': True}), patch('app.camera.os.open', return_value=42), patch('app.camera.os.close') as close, patch('app.camera.fcntl.ioctl', side_effect=ioctl):
        result = camera.capabilities('/dev/video0')
        assert result['recommended'] == {'width':3840, 'height':2160, 'input_format':'mjpeg'}
        assert len(result['modes']) == 4
        close.assert_called_once_with(42)


def test_stepwise_resolution_detection_and_no_reported_modes():
    import errno
    def ioctl(fd, request, buffer, mutate):
        if struct.unpack_from('I', buffer)[0]:
            raise OSError(errno.EINVAL, 'End')
        if request == 0xC0405602:
            buffer[44:48] = b'MJPG'
        else:
            struct.pack_into('7I', buffer, 8, 3, 320, 4095, 16, 240, 2161, 16)
    with patch('app.camera.probe_device', return_value={'capture':True}), patch('app.camera.os.open', return_value=42), patch('app.camera.os.close'), patch('app.camera.fcntl.ioctl', side_effect=ioctl):
        assert camera.capabilities('/dev/video0')['recommended'] == {'width':4080, 'height':2160, 'input_format':'mjpeg'}
    with patch('app.camera.probe_device', return_value={'capture':True}), patch('app.camera.os.open', return_value=42), patch('app.camera.os.close'), patch('app.camera.fcntl.ioctl', side_effect=OSError(errno.ENOTTY, 'Unsupported')):
        assert camera.capabilities('/dev/video0')['recommended'] is None


def test_capabilities_api_validates_paths_and_reports_disconnected_camera(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/cameras/capabilities', params={'device':'/dev/video0'}).json()['recommended']['width'] == 1920
        for path in ('/etc/passwd', '/dev/video0/../etc', 'http://camera'):
            assert client.get('/api/cameras/capabilities', params={'device':path}).status_code == 422
    with TestClient(create_app(tmp_path, demo=False)) as client, patch('app.camera.probe_device', side_effect=FileNotFoundError('Disconnected')):
        result = client.get('/api/cameras/capabilities', params={'device':'/dev/video0'})
        assert result.status_code == 503
        assert 'manually' in result.json()['detail']
