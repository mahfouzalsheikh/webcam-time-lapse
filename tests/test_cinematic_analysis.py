import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import cinematic
from app.main import create_app
from app.service import Recorder
from test_cinematic_shots import sparse_scene


def add_scenes(rec):
    for i in range(6):
        path = rec.root / 'frames' / f'{i + 1:032x}.jpg'
        sparse_scene(i // 2, growth=i % 2).save(path)
        with rec.store.connect() as db:
            db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)',
                       (path.stem, i + 1, path.stat().st_size))


def test_preview_matches_export_thresholds_and_reuses_comparisons(tmp_path):
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        rec = app.state.recorder
        add_scenes(rec)
        with patch('app.service.cinematic.scene_changes', wraps=cinematic.scene_changes) as analyze:
            response = client.post('/api/cinematic-analysis', json={})
            assert response.status_code == 200
            preview = response.json()
            assert preview['frames'] == 6
            assert len(preview['changes']) == 5  # Keep every transition for lower thresholds.
            assert cinematic.reset_photos(preview['changes']) == [3, 5]
            assert client.post('/api/cinematic-analysis', json={}).json() == preview
            assert analyze.call_count == 1
        paths = sorted((rec.root / 'frames').glob('*.jpg'))
        for threshold in (0, 45, 70, 100):
            ranges = cinematic.scene_ranges(paths, lambda: None, threshold_percent=threshold)
            assert [start + 1 for start, _ in ranges[1:]] == cinematic.reset_photos(preview['changes'], threshold)
        assert not rec.store.rows('SELECT * FROM exports')
        assert not list((rec.root / 'exports').iterdir())


def test_preview_respects_range_exclusions_cutoff_and_project_and_invalidates_cache(tmp_path):
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        rec = app.state.recorder
        add_scenes(rec)
        with patch('app.service.cinematic.scene_changes', wraps=cinematic.scene_changes) as analyze:
            client.post('/api/cinematic-analysis', json={})
            result = client.post('/api/cinematic-analysis', json={'start_frame_id': f'{2:032x}', 'end_frame_id': f'{5:032x}'}).json()
            assert result['frames'] == 4
            assert cinematic.reset_photos(result['changes']) == [2, 4]
            assert analyze.call_count == 2
            client.patch('/api/frames/selection', json={'frame_ids': [f'{3:032x}', f'{4:032x}'], 'excluded': True})
            result = client.post('/api/cinematic-analysis', json={}).json()
            assert result['frames'] == 4
            assert cinematic.reset_photos(result['changes']) == [3]
            assert analyze.call_count == 3
            result = client.post('/api/cinematic-analysis', json={'cutoff': 2}).json()
            assert result['frames'] == 2
            assert len(result['changes']) == 1
            assert cinematic.reset_photos(result['changes']) == []
            assert analyze.call_count == 4
        other = client.post('/api/projects', json={'name': 'Empty project'}).json()['id']
        assert client.post(f'/api/projects/{other}/cinematic-analysis', json={}).json() == {'frames': 0, 'changes': []}
        assert client.post(f'/api/projects/{other}/cinematic-analysis', json={'start_frame_id': f'{1:032x}'}).status_code == 409
        assert client.post('/api/cinematic-analysis', json={'start_frame_id': f'{6:032x}', 'end_frame_id': f'{1:032x}'}).status_code == 409


def test_threshold_is_validated_saved_and_has_legacy_default(tmp_path):
    app = create_app(tmp_path, demo=True)
    async def queued(self):
        pass
    with TestClient(app) as client, patch.object(Recorder, 'run_exports', queued):
        client.post('/api/capture', json={})
        for invalid in (-1, 101, 12.5, '45', True):
            assert client.post('/api/exports', json={'cinematic_reset_threshold': invalid}).status_code == 422
        for threshold in (0, 45, 100):
            response = client.post('/api/exports', json={'cinematic_focus': True, 'cinematic_reset_threshold': threshold})
            assert response.status_code == 202
            assert response.json()['cinematic_reset_threshold'] == threshold
            assert client.get('/api/exports').json()[0]['cinematic_reset_threshold'] == threshold
        assert client.post('/api/exports', json={'cinematic_focus': True}).json()['cinematic_reset_threshold'] == 45


def test_simultaneous_preview_requests_share_one_comparison(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        add_scenes(rec)
        with patch('app.service.cinematic.scene_changes', wraps=cinematic.scene_changes) as analyze:
            a, b = await asyncio.gather(rec.analyze_cinematic_resets(), rec.analyze_cinematic_resets())
            assert a == b
            assert analyze.call_count == 1
    asyncio.run(scenario())


def test_orientation_changes_reset_even_at_high_threshold(tmp_path):
    changes = [dict(photo=2, score_percent=0, orientation_change=True),
               dict(photo=3, score_percent=60, orientation_change=False)]
    assert cinematic.reset_photos(changes, 100) == [2]
    assert cinematic.reset_photos(changes, 45) == [2, 3]


def test_lower_threshold_can_discover_previously_filtered_small_reframing(tmp_path):
    from PIL import ImageChops
    original = sparse_scene(1)
    shifted = ImageChops.offset(original, 6, 0)
    paths = []
    for i, image in enumerate((original, shifted, shifted)):
        path = tmp_path / f'{i}.png'
        image.save(path)
        paths.append(path)
    changes = cinematic.scene_changes(paths, lambda: None)
    assert len(changes) == 2
    assert 5 < changes[0]['score_percent'] < 45
    assert cinematic.reset_photos(changes, 45) == []
    assert cinematic.reset_photos(changes, 5) == [2]
    assert cinematic.reset_photos(changes, 0) == [2]  # Identical photos never become cuts.
