import asyncio
import shutil
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import camera
from app.main import create_app
from app.models import Settings
from app.projects import Projects
from app.service import Recorder


def delete(client, path):
    return client.request('DELETE', path, json={})


def test_photo_delete_removes_files_and_updates_counts_without_touching_other_projects(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        first, latest = [client.post('/api/capture', json={}).json()['id'] for _ in range(2)]
        other = client.post('/api/projects', json={'name': 'Other'}).json()['id']
        foreign = client.post(f'/api/projects/{other}/capture', json={}).json()['id']
        assert delete(client, f'/api/frames/{foreign}').status_code == 404
        assert delete(client, '/api/frames/not-a-photo').status_code == 422
        assert client.request('DELETE', f'/api/frames/{first}').status_code == 415
        assert client.request('DELETE', f'/api/frames/{first}', json={}, headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        client.patch('/api/frames/selection', json={'frame_ids': [latest], 'excluded': True})
        assert delete(client, f'/api/frames/{latest}').status_code == 200
        assert delete(client, f'/api/frames/{latest}').status_code == 404
        status = client.get('/api/status').json()
        assert status['frames']['count'] == 1 and status['frames']['excluded'] == 0
        assert status['latest']['id'] == first
        assert status['frames']['bytes'] == (tmp_path / 'frames' / f'{first}.jpg').stat().st_size
        for kind in ('frames', 'thumbs'):
            assert not (tmp_path / kind / f'{latest}.jpg').exists()
            assert client.get(f'/media/{kind}/{latest}.jpg').status_code == 404
        assert client.get('/api/timeline').json()['total'] == 1
        assert client.get(f'/api/projects/{other}/status').json()['frames']['count'] == 1
        assert delete(client, f'/api/frames/{first}').status_code == 200
        assert client.get('/api/status').json()['latest'] is None
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/status').json()['frames']['count'] == 0


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_photo_delete_blocks_queued_export_and_preserves_completed_video(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=640, height=480))
        frame = await rec.take_photo()
        await rec.export_gate.acquire()
        job = await rec.create_export()
        try:
            with pytest.raises(ValueError, match='export to finish'):
                await rec.delete_frame(frame['id'])
            assert (tmp_path / 'frames' / f"{frame['id']}.jpg").exists()
        finally:
            rec.export_gate.release()
        await rec.export_task
        video = tmp_path / 'exports' / f"{job['id']}.mp4"
        original = video.read_bytes()
        await rec.delete_frame(frame['id'])
        assert video.read_bytes() == original
        assert rec.store.rows('SELECT * FROM export_frames') == []
        assert rec.store.rows('SELECT status FROM exports')[0]['status'] == 'complete'
    asyncio.run(scenario())


def test_interrupted_photo_cleanup_finishes_on_restart(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        frame = await rec.take_photo()
        real_unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == tmp_path / 'frames' / f"{frame['id']}.jpg":
                raise PermissionError('Temporary filesystem failure')
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail), pytest.raises(PermissionError):
            await rec.delete_frame(frame['id'])
        assert rec.status()['frames']['count'] == 0
        assert rec.store.rows('SELECT * FROM deleted_frames')
        restarted = Recorder(tmp_path, True)
        await restarted.start()
        try:
            assert restarted.store.rows('SELECT * FROM deleted_frames') == []
            assert not (tmp_path / 'frames' / f"{frame['id']}.jpg").exists()
            assert not (tmp_path / 'thumbs' / f"{frame['id']}.jpg").exists()
        finally:
            await restarted.stop()
    asyncio.run(scenario())


def test_delete_projects_including_default_and_last_project_without_resurrection(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        original = client.post('/api/capture', json={}).json()['id']
        other = client.post('/api/projects', json={'name': 'Keep me'}).json()['id']
        other_frame = client.post(f'/api/projects/{other}/capture', json={}).json()['id']
        # Include video and temporary resources in the legacy project cleanup.
        (tmp_path / 'exports' / ('a' * 32 + '.mp4')).write_bytes(b'video')
        assert delete(client, '/api/projects/default').status_code == 200
        for name in ('frames', 'thumbs', 'previews', 'exports', 'state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm', 'recorder.lock'):
            assert not (tmp_path / name).exists()
        assert (tmp_path / 'projects.sqlite3').exists()
        assert client.get('/api/status').status_code == 404
        assert client.get(f'/media/frames/{original}.jpg').status_code == 404
        assert client.get(f'/media/projects/{other}/frames/{other_frame}.jpg').status_code == 200
        assert client.get('/health').status_code == 200
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert [p['id'] for p in client.get('/api/projects').json()['projects']] == [other]
        assert delete(client, f'/api/projects/{other}').status_code == 200
        assert not (tmp_path / 'projects' / other).exists()
        assert delete(client, f'/api/projects/{other}').status_code == 404
        assert client.get('/api/projects').json()['projects'] == []
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/projects').json()['projects'] == []
        assert client.get('/health').status_code == 200
        assert client.post('/api/projects', json={'name': 'Fresh start'}).status_code == 201


def test_project_delete_waits_for_capture_and_cancels_queued_export(tmp_path):
    async def scenario():
        manager = Projects(tmp_path, True)
        new = await manager.create(Settings(name='Delete me'))
        rec = manager.get(new['id'])
        await manager.start()
        await rec.take_photo()
        await manager.export_gate.acquire()
        await rec.create_export()
        entered, release = threading.Event(), threading.Event()
        real_capture = camera.capture
        def blocked_capture(*args):
            entered.set()
            assert release.wait(5)
            return real_capture(*args)
        try:
            with patch('app.camera.capture', side_effect=blocked_capture):
                capture = asyncio.create_task(rec.take_photo())
                assert await asyncio.to_thread(entered.wait, 5)
                deletion = asyncio.create_task(manager.delete(new['id']))
                await asyncio.sleep(.05)
                assert manager.get(new['id']) is None
                assert not deletion.done()
                with pytest.raises(ValueError, match='being deleted'):
                    await rec.save_settings(Settings())
                release.set()
                await capture
                await deletion
            assert rec.scheduler_task.done() and rec.export_task.done()
            assert not rec.root.exists()
            with pytest.raises(ValueError, match='being deleted'):
                await rec.take_photo()
            assert manager.get('default') is not None
        finally:
            release.set()
            manager.export_gate.release()
            await manager.stop()
    asyncio.run(scenario())


def test_project_delete_stops_active_export_before_removing_files(tmp_path):
    async def scenario():
        manager = Projects(tmp_path, True)
        rec = manager.get('default')
        await rec.take_photo()
        entered = threading.Event()
        def export(job, settings):
            temporary = rec.root / 'exports' / '.in-progress.mp4'
            temporary.write_bytes(b'partial video')
            entered.set()
            assert rec.stopping.wait(5)
            assert temporary.exists()  # Cleanup must wait for this worker to finish.
        with patch.object(rec, 'export_sync', side_effect=export):
            await rec.create_export()
            assert await asyncio.to_thread(entered.wait, 5)
            await manager.delete('default')
        assert rec.export_task.done()
        assert not (tmp_path / 'exports').exists()
    asyncio.run(scenario())


def test_browser_disconnect_does_not_cancel_project_cleanup(tmp_path):
    async def scenario():
        manager = Projects(tmp_path, True)
        entered, release = asyncio.Event(), asyncio.Event()
        rec = manager.get('default')
        real_stop = rec.stop
        async def delayed_stop():
            entered.set()
            await release.wait()
            await real_stop()
        with patch.object(rec, 'stop', side_effect=delayed_stop):
            request = asyncio.create_task(manager.delete('default'))
            await entered.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not manager.deletions['default'].cancelled()
            release.set()
            await manager.deletions['default']
        assert manager.list() == []
        assert not (tmp_path / 'state.sqlite3').exists()
    asyncio.run(scenario())


@pytest.mark.parametrize('legacy', [True, False])
def test_interrupted_project_cleanup_finishes_on_restart(tmp_path, legacy):
    async def scenario():
        manager = Projects(tmp_path, True)
        other = await manager.create(Settings(name='Other'))
        key = 'default' if legacy else other['id']
        keep = other['id'] if legacy else 'default'
        await manager.get(key).take_photo()
        await manager.get(keep).take_photo()
        await manager.start()
        try:
            with patch('app.projects.shutil.rmtree', side_effect=OSError('Interrupted cleanup')):
                with pytest.raises(OSError):
                    await manager.delete(key)
            assert manager.get(key) is None
        finally:
            await manager.stop()
        restarted = Projects(tmp_path, True)
        await restarted.start()
        try:
            assert restarted.get(key) is None
            assert restarted.get(keep).status()['frames']['count'] == 1
            assert not ((tmp_path / 'frames') if legacy else (tmp_path / 'projects' / key)).exists()
            with restarted.connect() as db:
                assert db.execute('SELECT id FROM projects WHERE deleting=1').fetchall() == []
        finally:
            await restarted.stop()
    asyncio.run(scenario())
