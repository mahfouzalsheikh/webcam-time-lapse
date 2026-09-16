import asyncio
import shutil
import subprocess
from statistics import pstdev
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageStat

from app.lighting import prepare_frames
from app.main import create_app
from app.models import Settings
from app.service import Recorder


def add_frame(rec, number, color, excluded=False):
    path = rec.root / 'frames' / f'{number:032x}.jpg'
    Image.new('RGB', (320, 240), color).save(path, quality=95)
    with rec.store.connect() as db:
        db.execute('INSERT INTO frames(id,captured_at,bytes,excluded) VALUES (?,?,?,?)',
                   (path.stem, number, path.stat().st_size, excluded))
    return path


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_render_reduces_brightness_and_color_flicker_and_preserves_originals(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240, export_fps=4))
        paths = [add_frame(rec, index + 1, color) for index, color in enumerate(
            [(90, 80, 65), (130, 150, 165), (95, 85, 70), (135, 155, 170)] * 3)]
        # Neither excluded nor later captures should influence the reference.
        paths += [add_frame(rec, 13, (220, 50, 20), excluded=True),
                  add_frame(rec, 14, (20, 240, 240))]
        originals = [path.read_bytes() for path in paths]
        outputs = []
        for enabled in (False, True):
            job = await rec.create_export(cutoff=13, normalize_lighting=enabled)
            await rec.export_task
            result = rec.store.rows('SELECT * FROM exports WHERE id=?', (job['id'],))[0]
            assert result['status'] == 'complete', result['error']
            decoded = subprocess.run(
                ['ffmpeg', '-v', 'error', '-i', str(tmp_path / 'exports' / f"{job['id']}.mp4"),
                 '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-threads', '1', '-'],
                check=True, capture_output=True).stdout
            stride = 320 * 240 * 3
            assert len(decoded) == stride * 12
            means = [ImageStat.Stat(Image.frombytes('RGB', (320, 240), decoded[i:i + stride])).mean
                     for i in range(0, len(decoded), stride)]
            outputs.append(means)
        raw, normalized = outputs
        brightness = lambda values: pstdev([sum(rgb) / 3 for rgb in values])
        color_cast = lambda values: pstdev([rgb[0] / rgb[2] for rgb in values])
        assert brightness(normalized) < brightness(raw) * .15
        assert color_cast(normalized) < color_cast(raw) * .15
        assert [path.read_bytes() for path in paths] == originals
        assert not list((tmp_path / 'exports').glob('.*.lighting'))
    asyncio.run(scenario())


@pytest.mark.parametrize('colors', [[(0, 0, 0)], [(255, 255, 255)], [(20, 140, 50)],
                                  [(0, 0, 0), (100, 120, 110), (255, 255, 255)]])
def test_single_frame_and_extreme_exposures_are_safe(tmp_path, colors):
    rec = Recorder(tmp_path, True)
    paths = [add_frame(rec, i, color) for i, color in enumerate(colors)]
    output = tmp_path / 'corrected'
    output.mkdir()
    prepare_frames(paths, output, (320, 240), lambda: None)
    for path in paths:
        with Image.open(path) as original, Image.open(output / f'{path.stem}.png') as result:
            assert result.size == (320, 240)
            if len(colors) == 1:
                assert result.tobytes() == original.convert('RGB').tobytes()


@pytest.mark.parametrize('failure,status', [(InterruptedError('stopping'), 'queued'),
                                          (RuntimeError('disk reserve'), 'failed')])
def test_normalization_failure_cleans_temporary_files(tmp_path, failure, status):
    async def scenario():
        rec = Recorder(tmp_path, True)
        add_frame(rec, 1, (100, 120, 110))
        def fail(paths, directory, size, check):
            (directory / 'partial.png').write_bytes(b'partial')
            raise failure
        with patch('app.service.lighting.prepare_frames', side_effect=fail):
            job = await rec.create_export(normalize_lighting=True)
            await rec.export_task
        assert rec.store.rows('SELECT status FROM exports')[0]['status'] == status
        assert not list((tmp_path / 'exports').iterdir())
        assert (tmp_path / 'frames' / f'{1:032x}.jpg').exists()
    asyncio.run(scenario())


@pytest.mark.parametrize('payload,enabled', [({}, False), ({'normalize_lighting': True}, True)])
def test_export_api_records_optional_lighting_choice(tmp_path, payload, enabled):
    app = create_app(tmp_path, demo=True)
    async def leave_queued(self):
        pass
    with TestClient(app) as client, patch.object(Recorder, 'run_exports', leave_queued):
        client.post('/api/capture', json={})
        result = client.post('/api/exports', json=payload)
        assert result.status_code == 202
        assert result.json()['normalize_lighting'] is enabled
        assert bool(client.get('/api/exports').json()[0]['normalize_lighting']) is enabled
        assert client.post('/api/exports', json={'normalize_lighting': 'invalid'}).status_code == 422
