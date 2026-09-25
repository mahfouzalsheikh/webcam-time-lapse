import asyncio
import json
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from app.models import Settings
from app.service import Recorder


def seed(rec, count=6, same_time=False):
    ids = [f'{i + 1:032x}' for i in range(count)]
    for i, frame_id in enumerate(ids):
        path = rec.root / 'frames' / f'{frame_id}.jpg'
        Image.new('RGB', (320, 240), (60 + i % 120, 100, 80)).save(path)
        with rec.store.connect() as db:
            db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)',
                       (frame_id, 1 if same_time else i + 1, path.stat().st_size))
    return ids


def test_ranges_keep_photo_boundaries_across_exclusions_and_pagination(tmp_path):
    rec = Recorder(tmp_path, True)
    ids = seed(rec, 110)
    rec.select_frames([ids[2]], True)
    result = rec.timeline(cutoff=110, offset=100, limit=10, start_frame_id=ids[1], end_frame_id=ids[104])
    assert len(result['frames']) == 10
    assert result['range'] == {'included': 103, 'start_index': 1, 'end_index': 103}
    # Removing a photo before the range moves the index, not the chosen endpoint.
    rec.select_frames([ids[0], ids[1], ids[104]], True)
    result = rec.timeline(cutoff=110, start_frame_id=ids[1], end_frame_id=ids[104])
    assert result['range'] == {'included': 101, 'start_index': 0, 'end_index': 100}
    assert result['included'] == 106
    assert rec.timeline(start_frame_id=ids[108])['range']['included'] == 2
    assert rec.timeline(end_frame_id=ids[4])['range']['included'] == 2


def test_range_api_rejects_unknown_reversed_foreign_and_post_cutoff_endpoints(tmp_path):
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        ids = seed(app.state.recorder)
        other = client.post('/api/projects', json={'name': 'Other range'}).json()['id']
        foreign = client.post(f'/api/projects/{other}/capture', json={}).json()['id']
        for bounds in ({'start_frame_id': 'f' * 32}, {'end_frame_id': foreign},
                       {'start_frame_id': ids[4], 'end_frame_id': ids[1]},
                       {'cutoff': 3, 'end_frame_id': ids[4]}):
            assert client.post('/api/exports', json=bounds).status_code == 409
            assert client.get('/api/timeline', params=bounds).status_code == 409
        assert client.post('/api/exports', json={'start_frame_id': '../bad'}).status_code == 422
        assert client.get('/api/timeline', params={'end_frame_id': 'bad'}).status_code == 422
        assert client.get('/api/exports').json() == []


def test_inclusive_single_photo_and_identical_capture_timestamps(tmp_path):
    app = create_app(tmp_path, demo=True)
    async def queued(self):
        pass
    with TestClient(app) as client, patch.object(Recorder, 'run_exports', queued):
        ids = seed(app.state.recorder, same_time=True)
        bounds = {'start_frame_id': ids[2], 'end_frame_id': ids[2]}
        result = client.get('/api/timeline', params=bounds).json()
        assert result['range'] == {'included': 1, 'start_index': 2, 'end_index': 2}
        job = client.post('/api/exports', json={**bounds, 'interpolation': 'blend', 'intermediate_frames': 5}).json()
        assert job['frames'] == job['output_frames'] == 1
        saved = app.state.recorder.store.rows('SELECT frame_id FROM export_frames WHERE export_id=?', (job['id'],))
        assert saved == [{'frame_id': ids[2]}]
        assert client.get('/api/exports').json()[0]['start_frame_id'] == ids[2]
        app.state.recorder.select_frames([ids[2]], True)
        assert client.get('/api/timeline', params=bounds).json()['range']['included'] == 0
        assert client.post('/api/exports', json=bounds).status_code == 409


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_trimmed_export_snapshot_and_interpolation_survive_restart(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240))
        ids = seed(rec)
        rec.select_frames([ids[2]], True)
        await rec.export_gate.acquire()
        job = await rec.create_export(cutoff=5, start_frame_id=ids[1], end_frame_id=ids[3],
                                      interpolation='motion', intermediate_frames=5, fps=30,
                                      normalize_lighting=True)
        assert job['frames'] == 2 and job['output_frames'] == 7
        assert job['duration_seconds'] == pytest.approx(7 / 30)
        snapshot = rec.store.rows('SELECT frame_id FROM export_frames WHERE export_id=? ORDER BY frame_id', (job['id'],))
        assert [row['frame_id'] for row in snapshot] == [ids[1], ids[3]]
        rec.select_frames([ids[1]], True)
        rec.select_frames([ids[2]], False)
        rec.export_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rec.export_task
        rec.export_gate.release()
        with rec.store.connect() as db:
            db.execute("UPDATE exports SET status='running' WHERE id=?", (job['id'],))
        resumed = Recorder(tmp_path, True)
        await resumed.start()
        try:
            await resumed.export_task
            stored = resumed.store.rows('SELECT * FROM exports')[0]
            assert stored['status'] == 'complete', stored['error']
            assert stored['start_frame_id'] == ids[1] and stored['end_frame_id'] == ids[3]
            video = tmp_path / 'exports' / f"{job['id']}.mp4"
            stream = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(video)]))['streams'][0]
            assert int(stream['nb_frames']) == 7
            assert float(stream['duration']) == pytest.approx(7 / 30, abs=.001)
            assert len(list((tmp_path / 'frames').glob('*.jpg'))) == 6
        finally:
            await resumed.stop()
    asyncio.run(scenario())
