import asyncio
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import camera
from app.main import create_app
from app.models import Settings
from app.projects import Projects
from app.service import Recorder


def test_project_isolation_and_legacy_data_migration(tmp_path):
    original = Recorder(tmp_path, demo=True)
    asyncio.run(original.save_settings(Settings(name='Existing basil')))
    frame = asyncio.run(original.take_photo())
    with TestClient(create_app(tmp_path, demo=True)) as client:
        listing = client.get('/api/projects').json()['projects']
        assert len(listing) == 1
        assert listing[0]['settings']['name'] == 'Existing basil'
        assert listing[0]['frames']['count'] == 1
        new = client.post('/api/projects', json={'name': 'Tomatoes', 'interval_minutes': 5}).json()
        key = new['id']
        assert not new['runtime']['running']
        assert new['frames']['count'] == 0
        assert client.get(f'/media/projects/{key}/frames/{frame["id"]}.jpg').status_code == 404
        assert client.get(f'/media/projects/default/frames/{frame["id"]}.jpg').status_code == 200
        assert client.post(f'/api/projects/{key}/capture', json={}).status_code == 200
        assert client.get('/api/projects/default/frames').json()[0]['id'] == frame['id']
        assert client.get(f'/api/projects/{key}/status').json()['frames']['count'] == 1
        assert client.get('/api/projects/not-a-project/status').status_code == 404
        assert client.post('/api/projects', json={'interval_minutes': 0}).status_code == 422
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert len(client.get('/api/projects').json()['projects']) == 2
        assert client.get(f'/api/projects/{key}/status').json()['settings']['name'] == 'Tomatoes'
        assert client.get(f'/api/projects/{key}/status').json()['frames']['count'] == 1


def test_multiple_running_projects_resume_and_paused_project_stays_paused(tmp_path):
    async def scenario():
        manager = Projects(tmp_path, True)
        second = await manager.create(Settings(name='Second'))
        third = await manager.create(Settings(name='Paused'))
        for key in ('default', second['id']):
            rec = manager.get(key)
            await rec.set_running(True)
            await rec.tick()
        restarted = Projects(tmp_path, True)
        await restarted.start()
        try:
            for rec in restarted.recorders.values():
                await rec.tick()
            assert restarted.get('default').status()['frames']['count'] == 2
            assert restarted.get(second['id']).status()['frames']['count'] == 2
            assert restarted.get(third['id']).status()['frames']['count'] == 0
            assert not restarted.get(third['id']).status()['runtime']['running']
            assert all(rec.camera_lock is restarted.camera_lock for rec in restarted.recorders.values())
        finally:
            await restarted.stop()
    asyncio.run(scenario())


def test_shared_camera_is_serialized_between_projects(tmp_path):
    active = 0
    maximum = 0
    lock = threading.Lock()
    real_capture = camera.capture
    def capture(*args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(.03)
            return real_capture(*args)
        finally:
            with lock:
                active -= 1
    async def scenario():
        manager = Projects(tmp_path, True)
        new = await manager.create(Settings(name='Second'))
        recs = [manager.get('default'), manager.get(new['id'])]
        for rec in recs:
            await rec.set_running(True)
        with patch('app.camera.capture', side_effect=capture):
            await asyncio.gather(*(rec.tick() for rec in recs))
        assert maximum == 1
        assert [rec.status()['frames']['count'] for rec in recs] == [1, 1]
    asyncio.run(scenario())


def test_failure_retries_quickly_and_does_not_stop_other_project(tmp_path):
    async def scenario():
        manager = Projects(tmp_path, True)
        second = await manager.create(Settings(name='Healthy', camera_device='/dev/video2'))
        real = camera.capture
        def capture(path, settings, demo):
            if settings.camera_device == '/dev/video0':
                raise RuntimeError('Camera disconnected')
            real(path, settings, demo)
        for rec in manager.recorders.values():
            await rec.set_running(True)
        with patch('app.camera.capture', side_effect=capture):
            results = await asyncio.gather(*(rec.tick() for rec in manager.recorders.values()), return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        failed = manager.get('default').status()
        assert failed['runtime']['running']
        assert 0 < failed['runtime']['next_capture_at'] - time.time() <= 60
        assert manager.get(second['id']).status()['frames']['count'] == 1
        rec = manager.get('default')
        state = rec.store.get('runtime')
        state['next_capture_at'] = time.time() - 1
        rec.store.put('runtime', state)
        await rec.tick()
        assert rec.status()['runtime']['last_error'] is None
    asyncio.run(scenario())


def test_restart_respects_capture_window_and_end_date(tmp_path):
    async def scenario():
        manager = Projects(tmp_path, True)
        second = await manager.create(Settings(name='Finished'))
        for rec in manager.recorders.values():
            await rec.set_running(True)
        ended = manager.get(second['id'])
        state = ended.store.get('runtime')
        state['ends_at'] = time.time() - 1
        ended.store.put('runtime', state)
        restarted = Projects(tmp_path, True)
        with patch('app.service.in_window', return_value=False), patch('app.service.next_allowed', side_effect=lambda now, settings: now + 3600):
            await restarted.start()
            try:
                for rec in restarted.recorders.values():
                    await rec.tick()
                assert restarted.get('default').status()['frames']['count'] == 0
                assert restarted.get('default').status()['runtime']['running']
                assert not restarted.get(second['id']).status()['runtime']['running']
            finally:
                await restarted.stop()
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_interrupted_export_resumes_with_original_snapshot_and_settings(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=640, height=480, export_fps=2))
        await rec.take_photo()
        cutoff = time.time()
        export_id = 'a' * 32
        with rec.store.connect() as db:
            db.execute("INSERT INTO exports(id,created_at,status,frames,fps,error,settings) VALUES (?,?, 'running',1,2,NULL,?)", (export_id, cutoff, rec.settings().model_dump_json()))
        await rec.save_settings(Settings(width=1280, height=720, export_fps=30))
        await rec.take_photo()
        resumed = Recorder(tmp_path, True)
        await resumed.start()
        try:
            await resumed.export_task
            result = resumed.store.rows('SELECT * FROM exports')[0]
            assert result['status'] == 'complete', result['error']
            import subprocess
            probe = subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',str(tmp_path / 'exports' / f'{export_id}.mp4')],capture_output=True,check=True)
            video = json.loads(probe.stdout)['streams'][0]
            assert video['width'] == 640
            assert int(video['nb_frames']) == 1
            assert float(video['duration']) == pytest.approx(.5, abs=.1)
        finally:
            await resumed.stop()
    asyncio.run(scenario())


def test_hotplug_discovery_reads_current_host_device_directory(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMERA_DEVICE_ROOT', str(tmp_path))
    assert camera.list_devices() == []
    (tmp_path / 'video0').touch()
    with patch('app.camera.probe_device', return_value={'name':'New USB camera', 'capture':True}):
        assert camera.list_devices()[0]['id'] == '/dev/video0'
        assert camera.device_path('/dev/video0') == tmp_path / 'video0'
    (tmp_path / 'video0').unlink()
    assert camera.list_devices() == []


def test_legacy_export_schema_is_migrated_without_losing_rows(tmp_path):
    db = sqlite3.connect(tmp_path / 'state.sqlite3')
    db.execute('CREATE TABLE exports (id TEXT PRIMARY KEY, created_at REAL, status TEXT, frames INTEGER, fps INTEGER, error TEXT)')
    db.execute("INSERT INTO exports VALUES ('old',1,'complete',10,24,NULL)")
    db.commit()
    db.close()
    manager = Projects(tmp_path, True)
    rows = manager.get('default').store.rows('SELECT * FROM exports')
    assert rows[0]['id'] == 'old' and rows[0]['settings'] is None
