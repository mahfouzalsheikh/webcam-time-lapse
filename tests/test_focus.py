import errno
import struct
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import camera
from app.main import create_app
from app.models import Settings


@pytest.mark.parametrize('original', [0, 1])
def test_autofocus_runs_before_capture_and_restores_original_control(tmp_path, original):
    calls = []
    def ioctl(fd, request, buffer, mutate):
        assert fd == 42
        if request == camera.QUERYCTRL:
            assert struct.unpack_from('I', buffer)[0] == camera.FOCUS_AUTO
        elif request == camera.G_CTRL:
            struct.pack_into('i', buffer, 4, original)
        else:
            assert request == camera.S_CTRL
            calls.append(struct.unpack_from('i', buffer, 4)[0])
    def ffmpeg(command, **kwargs):
        assert calls == [0, 1]
        assert command.index('-ss') > command.index('-i')
        assert command[command.index('-ss') + 1] == '5'
        assert kwargs['timeout'] == 35
        calls.append('capture')
        Image.new('RGB', (640,480)).save(command[-1], 'JPEG')
        return SimpleNamespace(returncode=0)
    with patch('app.camera.probe_device', return_value={'capture':True}), patch('app.camera.os.open', return_value=42), patch('app.camera.os.close') as close, patch('app.camera.fcntl.ioctl', side_effect=ioctl), patch('app.camera.subprocess.run', side_effect=ffmpeg):
        camera.capture(tmp_path/'photo.jpg', Settings(focus_settle_seconds=5), False)
        assert calls == [0, 1, 'capture', original]
        close.assert_called_once_with(42)


def test_unsupported_camera_keeps_warmup_and_does_not_write_controls():
    with patch('app.camera.os.open', return_value=42), patch('app.camera.os.close') as close, patch('app.camera.fcntl.ioctl', side_effect=OSError(errno.EINVAL, 'Unsupported')) as ioctl:
        with camera.prepare_focus(Settings(focus_settle_seconds=15, warmup_seconds=2)) as wait:
            assert wait == 2
        assert ioctl.call_count == 1
        close.assert_called_once_with(42)


def test_disabled_option_does_not_touch_camera_controls():
    with patch('app.camera.os.open') as open_device:
        with camera.prepare_focus(Settings(autofocus=False, warmup_seconds=7)) as wait:
            assert wait == 7
        open_device.assert_not_called()


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired('ffmpeg', 33), RuntimeError('Capture failed')])
def test_focus_is_restored_when_capture_fails(error):
    def ioctl(fd, request, buffer, mutate):
        if request == camera.G_CTRL:
            struct.pack_into('i', buffer, 4, 0)
    with patch('app.camera.os.open', return_value=42), patch('app.camera.os.close') as close, patch('app.camera.fcntl.ioctl', side_effect=ioctl), patch('app.camera.set_focus_control') as set_control:
        with pytest.raises(type(error)):
            with camera.prepare_focus(Settings(warmup_seconds=10)) as wait:
                assert wait == 10  # Never shorten exposure warm-up.
                raise error
        assert [call.args for call in set_control.call_args_list] == [(42,0),(42,1),(42,0)]
        close.assert_called_once_with(42)


def test_focus_setup_failure_restores_control_and_is_actionable():
    def ioctl(fd, request, buffer, mutate):
        if request == camera.G_CTRL:
            struct.pack_into('i', buffer, 4, 1)
    with patch('app.camera.os.open', return_value=42), patch('app.camera.os.close') as close, patch('app.camera.fcntl.ioctl', side_effect=ioctl), patch('app.camera.set_focus_control', side_effect=[None, OSError(errno.EIO,'USB error'), None]) as set_control:
        with pytest.raises(RuntimeError, match='turn off autofocus'):
            with camera.prepare_focus(Settings()):
                pytest.fail('Must not capture after autofocus setup fails')
        assert set_control.call_args_list[-1].args == (42,1)
        close.assert_called_once_with(42)


def test_detection_is_read_only_and_ignores_disabled_controls():
    def ioctl(fd, request, buffer, mutate):
        assert request == camera.QUERYCTRL
        cid = struct.unpack_from('I', buffer)[0]
        if cid == camera.FOCUS_ABSOLUTE:
            struct.pack_into('I', buffer, 56, 1)
        if cid == camera.FOCUS_START:
            raise OSError(errno.EINVAL, 'Unsupported')
    with patch('app.camera.probe_device', return_value={'capture':True}), patch('app.camera.os.open', return_value=42), patch('app.camera.os.close'), patch('app.camera.fcntl.ioctl', side_effect=ioctl):
        assert camera.focus_capabilities('/dev/video0') == {'autofocus':True, 'single_shot':False, 'manual':False, 'demo':False}


def test_focus_api_settings_migration_and_persistence(tmp_path):
    app = create_app(tmp_path, demo=True)
    old = app.state.recorder.store.get('settings')
    old.pop('autofocus'); old.pop('focus_settle_seconds')
    app.state.recorder.store.put('settings', old)
    with TestClient(app) as client:
        settings = client.get('/api/status').json()['settings']
        assert settings['autofocus'] is True and settings['focus_settle_seconds'] == 3
        assert client.get('/api/cameras/focus?device=/dev/video0').json()['demo'] is True
        assert client.get('/api/cameras/focus?device=/etc/passwd').status_code == 422
        for value in (0,16):
            assert client.put('/api/settings', json={**settings,'focus_settle_seconds':value}).status_code == 422
        settings.update(autofocus=False,focus_settle_seconds=8)
        assert client.put('/api/settings', json=settings).status_code == 200
        candidate = {**settings,'autofocus':True,'focus_settle_seconds':4}
        assert client.post('/api/preview', json={'settings':candidate}).status_code == 200
        assert client.get('/api/status').json()['settings'] == settings
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/status').json()['settings'] == settings
