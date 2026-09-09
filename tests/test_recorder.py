import asyncio
import json
import shutil
import subprocess
import time
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Settings
from app.service import Recorder, in_window, next_allowed


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        yield client


def test_capture_preview_settings_and_persistence(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/health').status_code == 200
        assert not client.get('/api/status').json()['runtime']['running']
        preview = client.post('/api/preview', json={})
        assert preview.headers['content-type'] == 'image/jpeg'
        assert preview.content.startswith(b'\xff\xd8')
        assert client.get('/api/frames').json() == []
        frame = client.post('/api/capture', json={}).json()
        assert client.get(f"/media/frames/{frame['id']}.jpg").status_code == 200
        assert client.get(f"/media/thumbs/{frame['id']}.jpg").status_code == 200
        settings = client.get('/api/status').json()['settings']
        settings.update(name='Monstera', timezone='America/Toronto', interval_minutes=60)
        assert client.put('/api/settings', json=settings).status_code == 200
        assert client.post('/api/start', json={}).status_code == 200
        assert client.post('/api/pause', json={}).status_code == 200
        before = client.get('/api/status').json()
    with TestClient(create_app(tmp_path, demo=True)) as client:
        after = client.get('/api/status').json()
        assert after['settings']['name'] == 'Monstera'
        assert after['frames']['count'] >= 1
        assert after['runtime'] == before['runtime']
        assert not list((tmp_path / 'frames').glob('.*.jpg'))


@pytest.mark.parametrize('change', [
    {'interval_minutes': 0}, {'width': 321}, {'timezone': 'Not/A_Zone'},
    {'day_start': '25:00'}, {'daylight_only': True, 'day_start': '07:00', 'day_end': '07:00'},
    {'input_format': ';rm -rf'}, {'reserve_mb': 0}, {'export_fps': 61},
])
def test_settings_validation(client, change):
    settings = client.get('/api/status').json()['settings']
    settings.update(change)
    assert client.put('/api/settings', json=settings).status_code == 422


def test_browser_boundary_and_media_paths(client):
    assert client.post('/api/start', json={}, headers={'Origin': 'https://unrelated.example'}).status_code == 403
    assert client.post('/api/start', content='{}').status_code == 415
    assert client.get('/media/frames/state.sqlite3').status_code == 404
    assert client.get('/media/exports/.123.mp4').status_code == 404
    assert client.get('/api/frames?limit=100000').status_code == 422


def test_camera_failure_and_recovery(client):
    with patch('app.camera.capture', side_effect=RuntimeError('Webcam disconnected')):
        result = client.post('/api/capture', json={})
    assert result.status_code == 503
    assert 'Webcam disconnected' in client.get('/api/status').json()['runtime']['last_error']
    assert client.get('/api/frames').json() == []
    assert client.post('/api/capture', json={}).status_code == 200
    assert client.get('/api/status').json()['runtime']['last_error'] is None


def test_disk_reserve_preserves_existing_frames(client):
    client.post('/api/capture', json={})
    usage = shutil._ntuple_diskusage(1000000000, 999999999, 1)
    with patch('app.service.shutil.disk_usage', return_value=usage):
        assert client.post('/api/capture', json={}).status_code == 503
        assert client.post('/api/exports', json={}).status_code == 503
    assert len(client.get('/api/frames').json()) == 1


def stamp(iso):
    return datetime.fromisoformat(iso).timestamp()


def test_windows_and_dst():
    settings = Settings(daylight_only=True, timezone='America/Toronto')
    assert in_window(stamp('2026-09-08T07:00:00-04:00'), settings)
    assert not in_window(stamp('2026-09-08T19:00:00-04:00'), settings)
    due = next_allowed(stamp('2026-09-08T20:00:00-04:00'), settings)
    assert due == stamp('2026-09-09T07:00:00-04:00')
    overnight = Settings(daylight_only=True, day_start='20:00', day_end='06:00')
    assert in_window(stamp('2026-09-08T23:00:00+00:00'), overnight)
    assert in_window(stamp('2026-09-09T02:00:00+00:00'), overnight)
    assert not in_window(stamp('2026-09-09T12:00:00+00:00'), overnight)
    spring = Settings(daylight_only=True, timezone='America/Toronto', day_start='02:30', day_end='04:00')
    assert next_allowed(stamp('2026-03-08T01:59:00-05:00'), spring) == stamp('2026-03-08T03:00:00-04:00')


def test_missed_intervals_end_and_duration_extension(tmp_path):
    async def scenario():
        recorder = Recorder(tmp_path, demo=True)
        await recorder.set_running(True)
        state = recorder.store.get('runtime')
        state['next_capture_at'] = time.time() - 86400
        recorder.store.put('runtime', state)
        await recorder.tick()
        await recorder.tick()
        assert recorder.status()['frames']['count'] == 1
        assert recorder.status()['runtime']['next_capture_at'] > time.time()
        state = recorder.store.get('runtime')
        state['started_at'] = time.time() - 91 * 86400
        state['ends_at'] = time.time() - 86400
        recorder.store.put('runtime', state)
        await recorder.tick()
        assert not recorder.status()['runtime']['running']
        with pytest.raises(ValueError, match='ended'):
            await recorder.set_running(True)
        await recorder.save_settings(Settings(duration_days=120))
        await recorder.set_running(True)
        assert recorder.status()['runtime']['running']
    asyncio.run(scenario())


def test_exclusive_camera_and_paused_scheduled_capture(tmp_path):
    async def scenario():
        recorder = Recorder(tmp_path, demo=True)
        async with recorder.camera_lock:
            with pytest.raises(ValueError, match='busy'):
                await recorder.take_photo()
        assert await recorder.take_photo(scheduled=True) is None
        assert recorder.status()['frames']['count'] == 0
    asyncio.run(scenario())


def test_single_owner_and_interrupted_export(tmp_path):
    async def scenario():
        first, second = Recorder(tmp_path, True), Recorder(tmp_path, True)
        abandoned = tmp_path / 'exports' / ('.' + 'a' * 32 + '.mp4')
        abandoned.write_bytes(b'partial')
        original = tmp_path / 'frames' / ('b' * 32 + '.jpg')
        original.write_bytes(b'original')
        with first.store.connect() as db:
            db.execute("INSERT INTO exports(id,created_at,status,frames,fps,error) VALUES ('old',0,'running',1,24,NULL)")
        await first.start()
        try:
            with pytest.raises(RuntimeError, match='one worker'):
                await second.start()
            assert first.store.rows('SELECT status FROM exports')[0]['status'] == 'queued'
            assert not abandoned.exists()
            assert original.exists()
        finally:
            await first.stop()
    asyncio.run(scenario())


def test_running_study_takes_one_fresh_capture_after_restart(tmp_path):
    async def scenario():
        first = Recorder(tmp_path, True)
        await first.set_running(True)
        await first.tick()
        due = first.store.get('runtime')['next_capture_at']
        second = Recorder(tmp_path, True)
        await second.start()
        try:
            await second.tick()
            assert second.status()['runtime']['running']
            assert second.status()['runtime']['next_capture_at'] > due
            assert second.status()['frames']['count'] == 2
        finally:
            await second.stop()
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required for real video integration')
def test_real_export_snapshot_duration_and_mixed_resolutions(tmp_path):
    async def scenario():
        recorder = Recorder(tmp_path, demo=True)
        await recorder.save_settings(Settings(width=640, height=480, export_fps=2))
        for _ in range(2):
            await recorder.take_photo()
        await recorder.save_settings(Settings(width=1280, height=720, export_fps=2))
        await recorder.take_photo()
        job = await recorder.create_export()
        with pytest.raises(ValueError, match='already'):
            await recorder.create_export()
        await recorder.take_photo()
        await recorder.export_task
        result = recorder.store.rows('SELECT * FROM exports')[0]
        assert result['status'] == 'complete', result['error']
        assert result['frames'] == 3
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(tmp_path / 'exports' / f"{job['id']}.mp4")], capture_output=True, check=True)
        stream = json.loads(probe.stdout)['streams'][0]
        assert int(stream['nb_frames']) == 3
        assert float(stream['duration']) == pytest.approx(1.5, abs=.1)
        assert stream['width'] == 1280 and stream['height'] == 720
        assert stream['pix_fmt'] == 'yuv420p'
    asyncio.run(scenario())
