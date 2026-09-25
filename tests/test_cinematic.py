import asyncio
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw, ImageStat

from app import cinematic
from app.main import create_app
from app.models import Settings
from app.service import Recorder


def photos(directory):
    paths = []
    for i in range(4):
        frame = Image.new('RGB', (640, 360), (150, 150, 150))
        draw = ImageDraw.Draw(frame)
        for x in range(0, 640, 8):
            draw.line((x, 0, x, 359), fill=(100, 100, 100), width=2)
        draw.rectangle((250 + i * 10, 120 - i * 15, 275 + i * 10, 240), fill=(25, 110, 35))
        path = directory / f'{i + 1:032x}.png'
        frame.save(path)
        paths.append(path)
    return paths


def test_fixed_focus_preserves_changing_detail_and_softens_background(tmp_path):
    paths = photos(tmp_path)
    originals = [Image.open(path).convert('RGB') for path in paths]
    mask = cinematic.focus_mask(paths, lambda: None, lambda *args: None)
    assert mask is not None
    assert mask.getpixel((15, 15)) == 0
    updates = []
    cinematic.prepare_frames(paths, lambda: None, lambda *args: updates.append(args))
    backgrounds = []
    for original, path in zip(originals, paths):
        with Image.open(path) as result:
            # Entire moving/growing rectangle, including all original edges.
            difference = ImageChops.difference(original, result).crop((250, 115, 309, 238))
            assert max(ImageStat.Stat(difference).mean) < 1
            before = ImageStat.Stat(original.crop((20, 20, 100, 100)))
            after = ImageStat.Stat(result.crop((20, 20, 100, 100)))
            assert after.mean[0] < before.mean[0] * .8
            assert after.stddev[0] < before.stddev[0] * .2
            backgrounds.append(result.crop((20, 20, 100, 100)).tobytes())
    assert len(set(backgrounds)) == 1
    assert updates[-1] == ('focusing', 4, 4)
    assert not list(tmp_path.glob('*.focus.png'))


def test_broad_lighting_changes_do_not_become_a_subject_mask(tmp_path):
    paths = []
    for i in range(3):
        frame = Image.new('RGB', (320, 240))
        draw = ImageDraw.Draw(frame)
        for x in range(320):
            level = 50 + round(x / 4) + i * 20
            draw.line((x, 0, x, 239), fill=(level,) * 3)
        path = tmp_path / f'{i}.png'
        frame.save(path)
        paths.append(path)
    assert cinematic.focus_mask(paths, lambda: None, lambda *args: None) is None
    originals = [path.read_bytes() for path in paths]
    cinematic.prepare_frames(paths, lambda: None)
    assert [path.read_bytes() for path in paths] == originals


@pytest.mark.parametrize('sizes', [[(320, 240)], [(320, 240), (240, 320)]])
def test_no_motion_or_incompatible_framing_keeps_images_intact(tmp_path, sizes):
    paths = []
    for i, size in enumerate(sizes):
        path = tmp_path / f'{i}.png'
        Image.new('RGB', size, (120, 130, 140)).save(path)
        paths.append(path)
    originals = [path.read_bytes() for path in paths]
    cinematic.prepare_frames(paths, lambda: None)
    assert [path.read_bytes() for path in paths] == originals


def test_api_cinematic_choice_implies_lighting_and_is_saved(tmp_path):
    app = create_app(tmp_path, demo=True)
    async def queued(self):
        pass
    with TestClient(app) as client, patch.object(Recorder, 'run_exports', queued):
        client.post('/api/capture', json={})
        for enabled in (False, True):
            response = client.post('/api/exports', json={'cinematic_focus': enabled, 'normalize_lighting': False})
            assert response.status_code == 202
            assert response.json()['cinematic_focus'] is enabled
            assert response.json()['normalize_lighting'] is enabled
            saved = client.get('/api/exports').json()[0]
            assert bool(saved['cinematic_focus']) is enabled
            assert bool(saved['normalize_lighting']) is enabled
        assert client.post('/api/exports', json={'cinematic_focus': 'invalid'}).status_code == 422


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('mode', ['none', 'repeat', 'blend', 'motion'])
def test_cinematic_export_survives_restart_keeps_sources_and_frame_count(tmp_path, mode):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=640, height=360))
        inputs = tmp_path / 'inputs'
        inputs.mkdir()
        originals = []
        for i, path in enumerate(photos(inputs)):
            target = rec.root / 'frames' / f'{path.stem}.jpg'
            with Image.open(path) as image:
                image.save(target)
            originals.append((target, target.read_bytes()))
            with rec.store.connect() as db:
                db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)', (target.stem, 1 + i * 86400, target.stat().st_size))
        async def queued(self):
            pass
        with patch.object(Recorder, 'run_exports', queued):
            job = await rec.create_export(cinematic_focus=True, interpolation=mode, intermediate_frames=2, timing_overlay=True)
            await rec.export_task
        resumed = Recorder(tmp_path, True)
        await resumed.start()
        try:
            await resumed.export_task
        finally:
            await resumed.stop()
        saved = rec.store.rows('SELECT * FROM exports')[0]
        assert saved['status'] == 'complete', saved['error']
        assert saved['cinematic_focus'] and saved['normalize_lighting']
        video = tmp_path / 'exports' / f"{job['id']}.mp4"
        count = int(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                            '-show_entries', 'stream=nb_frames', '-of', 'default=nw=1:nk=1', str(video)]))
        assert count == job['output_frames'] == (4 if mode == 'none' else 10)
        assert all(path.read_bytes() == original for path, original in originals)
        assert all(path.suffix == '.mp4' for path in (tmp_path / 'exports').iterdir())
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,status', [(InterruptedError('stopping'), 'queued'), (RuntimeError('disk reserve'), 'failed')])
def test_failed_focus_export_removes_temporary_files(tmp_path, failure, status):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.take_photo()
        def fail(paths, *args):
            paths[0].with_suffix('.focus.png').write_text('partial')
            raise failure
        with patch('app.service.cinematic.prepare_frames', fail):
            await rec.create_export(cinematic_focus=True)
            await rec.export_task
        job = rec.store.rows('SELECT * FROM exports')[0]
        assert job['status'] == status and job['progress'] is None
        assert not list((tmp_path / 'exports').iterdir())
    asyncio.run(scenario())
