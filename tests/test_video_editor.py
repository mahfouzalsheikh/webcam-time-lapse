import asyncio
import json
import shutil
import sqlite3
import subprocess
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Settings
from app.service import Recorder


def test_selection_is_durable_reversible_and_project_scoped(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        ids = [client.post('/api/capture', json={}).json()['id'] for _ in range(3)]
        other = client.post('/api/projects', json={'name': 'Other plant'}).json()['id']
        foreign = client.post(f'/api/projects/{other}/capture', json={}).json()['id']
        # Reject the entire batch if it includes an unknown/other-project frame.
        assert client.patch('/api/frames/selection', json={'frame_ids': [ids[0], foreign], 'excluded': True}).status_code == 409
        assert client.get('/api/timeline').json()['excluded'] == 0
        result = client.patch('/api/frames/selection', json={'frame_ids': [ids[1], ids[1]], 'excluded': True})
        assert result.json()['updated'] == 1
        timeline = client.get('/api/timeline?limit=2&offset=1').json()
        assert timeline['total'] == 3 and timeline['included'] == 2 and timeline['excluded'] == 1
        assert [f['id'] for f in timeline['frames']] == ids[1:]
        assert timeline['frames'][-1]['video_index'] == 1
        included = client.get('/api/timeline?included_only=true').json()['frames']
        assert [f['id'] for f in included] == [ids[0], ids[2]]
        assert [f['video_index'] for f in included] == [0, 1]
        assert client.get('/api/status').json()['frames']['count'] == 3
        assert client.get(f'/media/frames/{ids[1]}.jpg').status_code == 200
        cutoff = timeline['cutoff']
        client.post('/api/capture', json={})
        assert client.get('/api/timeline', params={'cutoff': cutoff}).json()['total'] == 3
        assert client.get('/api/timeline').json()['total'] == 4
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/timeline').json()['excluded'] == 1
        assert client.get(f'/api/projects/{other}/timeline').json()['excluded'] == 0
        assert client.patch('/api/frames/selection', json={'frame_ids': [ids[1]], 'excluded': False}).status_code == 200
        assert client.get('/api/timeline').json()['excluded'] == 0


def test_empty_selection_and_invalid_timeline_requests(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        frame = client.post('/api/capture', json={}).json()['id']
        client.patch('/api/frames/selection', json={'frame_ids': [frame], 'excluded': True})
        assert client.post('/api/exports', json={}).status_code == 409
        assert client.get('/api/exports').json() == []
        for ids in ([], ['../bad'], ['a' * 32] * 1001):
            assert client.patch('/api/frames/selection', json={'frame_ids': ids, 'excluded': True}).status_code == 422
        for query in ('limit=101', 'offset=-1', 'cutoff=nan', 'cutoff=inf', 'cutoff=-1'):
            assert client.get('/api/timeline?' + query).status_code == 422
        assert client.post('/api/exports', json={'cutoff': -1}).status_code == 422


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('normalize_lighting', [False, True])
@pytest.mark.parametrize('interpolation', ['none', 'repeat', 'blend', 'motion'])
def test_export_keeps_reviewed_frames_and_settings_across_restart(tmp_path, normalize_lighting, interpolation):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=640, height=480, export_fps=2))
        ids = [(await rec.take_photo())['id'] for _ in range(3)]
        cutoff = time.time()
        later = (await rec.take_photo())['id']
        rec.select_frames([ids[1]], True)
        # Queue behind another export, then interrupt before FFmpeg starts.
        await rec.export_gate.acquire()
        job = await rec.create_export(cutoff, normalize_lighting=normalize_lighting, interpolation=interpolation, intermediate_frames=5, fps=4)
        rec.select_frames([ids[1]], False)
        rec.select_frames([ids[0]], True)
        await rec.save_settings(Settings(width=1280, height=720, export_fps=30))
        snapshot = rec.store.rows('SELECT frame_id FROM export_frames WHERE export_id=?', (job['id'],))
        assert {f['frame_id'] for f in snapshot} == {ids[0], ids[2]}
        assert later not in {f['frame_id'] for f in snapshot}
        rec.export_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rec.export_task
        rec.export_gate.release()
        with rec.store.connect() as db:
            db.execute("UPDATE exports SET status='running' WHERE id=?", (job['id'],))
        stale = tmp_path / 'exports' / f".{job['id']}.lighting"
        stale.mkdir()
        (stale / 'partial.png').write_bytes(b'interrupted')
        resumed = Recorder(tmp_path, True)
        await resumed.start()
        try:
            await resumed.export_task
            result = resumed.store.rows('SELECT * FROM exports')[0]
            assert result['status'] == 'complete', result['error']
            assert bool(result['normalize_lighting']) == normalize_lighting
            assert result['interpolation'] == interpolation
            assert result['intermediate_frames'] == (0 if interpolation == 'none' else 5)
            assert not stale.exists()
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(tmp_path / 'exports' / f"{job['id']}.mp4")], capture_output=True, check=True)
            stream = json.loads(probe.stdout)['streams'][0]
            assert (stream['width'], stream['height']) == (640, 480)
            expected = 2 if interpolation == 'none' else 7
            assert int(stream['nb_frames']) == expected
            assert stream['r_frame_rate'] == '4/1'
            assert float(stream['duration']) == pytest.approx(expected / 4, abs=.01)
            assert resumed.status()['frames']['count'] == 4
        finally:
            await resumed.stop()
    asyncio.run(scenario())


def test_original_frames_are_included_after_schema_upgrade(tmp_path):
    with sqlite3.connect(tmp_path / 'state.sqlite3') as db:
        db.execute('CREATE TABLE frames (id TEXT PRIMARY KEY, captured_at REAL NOT NULL, bytes INTEGER NOT NULL)')
        db.execute('INSERT INTO frames VALUES (?,1,1234)', ('a' * 32,))
    rec = Recorder(tmp_path, True)
    assert rec.timeline()['included'] == 1
    assert rec.timeline()['frames'][0]['excluded'] == 0
