import asyncio
import shutil
import sqlite3
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageStat

from app.main import create_app
from app.models import Settings
from app.service import Recorder
from app.timing_overlay import ass_time, elapsed_times, write_overlay


def seed(rec, timestamps):
    paths = []
    for i, timestamp in enumerate(timestamps, 1):
        path = rec.root / 'frames' / f'{i:032x}.jpg'
        Image.new('RGB', (320, 240), (100, 120, 140)).save(path)
        with rec.store.connect() as db:
            db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)',
                       (path.stem, timestamp, path.stat().st_size))
        paths.append(path)
    return paths


@pytest.mark.parametrize('mode,expected', [
    ('none', [0, 1800, 90000]),
    ('repeat', [0, 0, 0, 1800, 1800, 1800, 90000]),
    ('blend', [0, 600, 1200, 1800, 31200, 60600, 90000]),
    ('motion', [0, 600, 1200, 1800, 31200, 60600, 90000]),
])
def test_timer_maps_real_capture_gaps_to_output_frames(mode, expected):
    assert list(elapsed_times([100, 1900, 90100], mode, 2)) == expected
    assert list(elapsed_times([100], mode, 2)) == [0]
    assert list(elapsed_times([100, 100], mode, 2)) == [0] * (2 if mode == 'none' else 4)
    assert list(elapsed_times([], mode, 2)) == []


@pytest.mark.parametrize('fps', [1, 24, 30, 60])
def test_subtitle_events_cover_every_video_frame(fps):
    def seconds(value):
        h, m, s = value.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    for frame in range(fps * 3):
        actual = frame / fps
        assert seconds(ass_time(frame, fps)) <= actual < seconds(ass_time(frame + 1, fps))


def test_api_remembers_optional_timer_and_upgrades_old_exports(tmp_path):
    with sqlite3.connect(tmp_path / 'state.sqlite3') as db:
        db.execute('CREATE TABLE exports (id TEXT PRIMARY KEY,created_at REAL,status TEXT,frames INTEGER,fps INTEGER,error TEXT)')
        db.execute("INSERT INTO exports VALUES ('old',1,'complete',24,24,NULL)")
    app = create_app(tmp_path, demo=True)
    async def queued(self):
        pass
    with TestClient(app) as client, patch.object(Recorder, 'run_exports', queued):
        seed(app.state.recorder, [1, 86401])
        assert not client.get('/api/exports').json()[0]['timing_overlay']
        for enabled in (False, True):
            response = client.post('/api/exports', json={'timing_overlay': enabled})
            assert response.status_code == 202
            assert response.json()['timing_overlay'] is enabled
            assert bool(client.get('/api/exports').json()[0]['timing_overlay']) is enabled
        assert client.post('/api/exports', json={'timing_overlay': 'invalid'}).status_code == 422


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('mode,fps,count', [('none', 24, 3), ('repeat', 60, 3), ('blend', 30, 3), ('motion', 24, 3), ('repeat', 60, 1)])
def test_timer_renders_after_effects_with_exact_duration_and_clean_files(tmp_path, mode, fps, count):
    async def scenario():
        # Exercise both FFmpeg filtergraph escaping layers.
        root = tmp_path / "photos [test], 'clock': folder"
        rec = Recorder(root, True)
        await rec.save_settings(Settings(width=320, height=240))
        paths = seed(rec, [1, 48601, 135001][:count])
        originals = [path.read_bytes() for path in paths]
        decoded = []
        for enabled in (False, True):
            job = await rec.create_export(timing_overlay=enabled, normalize_lighting=True,
                                          interpolation=mode, intermediate_frames=2, fps=fps)
            await rec.export_task
            saved = rec.store.rows('SELECT * FROM exports WHERE id=?', (job['id'],))[0]
            assert saved['status'] == 'complete', saved['error']
            video = root / 'exports' / f"{job['id']}.mp4"
            raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video),
                                           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-threads', '1', '-'])
            stride = 320 * 240 * 3
            assert len(raw) == stride * job['output_frames']
            decoded.append([Image.frombytes('RGB', (320, 240), raw[n:n + stride]) for n in range(0, len(raw), stride)])
        # Overlay is present on every output frame, including endpoints, and
        # affects only the top-left area (allowing for H.264 rounding).
        for plain, clock in zip(*decoded):
            diff = ImageChops.difference(plain, clock)
            assert max(ImageStat.Stat(diff.crop((5, 5, 53, 63))).mean) > 20
            assert max(ImageStat.Stat(diff.crop((100, 100, 320, 240))).mean) < 2
        if count > 1:
            first, last = decoded[1][0].crop((5, 5, 53, 63)), decoded[1][-1].crop((5, 5, 53, 63))
            assert max(ImageStat.Stat(ImageChops.difference(first, last)).mean) > 2
        assert [path.read_bytes() for path in paths] == originals
        assert all(path.suffix == '.mp4' for path in (root / 'exports').iterdir())
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_trimmed_timer_uses_queued_selection_after_restart(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240))
        paths = seed(rec, [1, 3601, 7201, 93601])
        rec.select_frames([paths[2].stem], True)
        async def queued(self):
            pass
        with patch.object(Recorder, 'run_exports', queued):
            job = await rec.create_export(start_frame_id=paths[1].stem, end_frame_id=paths[3].stem,
                                          timing_overlay=True, interpolation='repeat', intermediate_frames=2)
            await rec.export_task
        rec.select_frames([paths[2].stem], False)
        stale = tmp_path / 'exports' / f".{job['id']}.timing.ass"
        stale.write_text('interrupted')
        resumed = Recorder(tmp_path, True)
        observed = []
        def tracked(path, timestamps, *args):
            observed.append(timestamps)
            return write_overlay(path, timestamps, *args)
        with patch('app.service.timing_overlay.write_overlay', tracked):
            await resumed.start()
            try:
                await resumed.export_task
            finally:
                await resumed.stop()
        saved = resumed.store.rows('SELECT * FROM exports WHERE id=?', (job['id'],))[0]
        assert saved['status'] == 'complete', saved['error']
        assert saved['timing_overlay'] == 1
        assert observed == [[3601, 93601]]
        assert not stale.exists()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure,status', [(InterruptedError('stopping'), 'queued'), (RuntimeError('disk reserve'), 'failed')])
def test_overlay_failure_cleans_up(tmp_path, failure, status):
    async def scenario():
        rec = Recorder(tmp_path, True)
        seed(rec, [1, 3601])
        def fail(path, *args):
            path.write_text('partial')
            raise failure
        with patch('app.service.timing_overlay.write_overlay', fail):
            await rec.create_export(timing_overlay=True)
            await rec.export_task
        saved = rec.store.rows('SELECT * FROM exports')[0]
        assert saved['status'] == status
        assert saved['progress'] is None
        assert not list((tmp_path / 'exports').iterdir())
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['none', 'repeat', 'blend', 'motion'])
def test_overall_progress_never_resets_at_hour_or_day_boundaries(mode):
    from app.timing_overlay import overall_progress

    timestamps = [0, 3599, 3600, 86399, 86400, 270000]
    fractions = [overall_progress(elapsed, timestamps[-1]) for elapsed in elapsed_times(timestamps, mode, 5)]
    assert fractions == sorted(fractions)
    assert fractions[0] == 0 and fractions[-1] == 1
    assert overall_progress(0, 0) == 1


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('size,orientation', [((600, 900), 1), ((1800, 600), 1), ((900, 600), 6)])
@pytest.mark.parametrize('normalize', [False, True])
def test_dial_stays_in_photo_area_with_only_circular_shading(tmp_path, size, orientation, normalize):
    from app.timing_overlay import photo_bounds

    async def scenario():
        rec = Recorder(tmp_path, True)
        settings = Settings(width=640, height=360)
        await rec.save_settings(settings)
        path = rec.root / 'frames' / f'{1:032x}.jpg'
        exif = Image.Exif()
        exif[274] = orientation
        Image.new('RGB', size, (180, 170, 150)).save(path, exif=exif)
        with rec.store.connect() as db:
            db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)', (path.stem, 1, path.stat().st_size))
        images = []
        for enabled in (False, True):
            job = await rec.create_export(timing_overlay=enabled, normalize_lighting=normalize)
            await rec.export_task
            result = rec.store.rows('SELECT status,error FROM exports WHERE id=?', (job['id'],))[0]
            assert result['status'] == 'complete', result['error']
            video = tmp_path / 'exports' / f"{job['id']}.mp4"
            raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video), '-frames:v', '1',
                                           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-threads', '1', '-'])
            images.append(Image.frombytes('RGB', (640, 360), raw))
        plain, clock = images
        left, top, width, height = photo_bounds([path], settings, lambda: None)
        difference = ImageChops.difference(plain, clock).convert('L')
        bbox = difference.point(lambda value: 255 if value > 15 else 0).getbbox()
        assert bbox is not None
        x0, y0, x1, y1 = bbox
        assert left + 4 < x0 < x1 < left + width - 4
        assert top + 4 < y0 < y1 < top + height - 4
        # The actual photo (not just the canvas) must be under the whole dial.
        for x, y in ((x0, y0), (x1 - 1, y1 - 1)):
            assert min(plain.getpixel((x, y))) > 100
        # Corners of the dial's enclosing square have no rectangular shade.
        for x, y in ((x0 + 2, y0 + 2), (x1 - 3, y0 + 2), (x0 + 2, y1 - 3), (x1 - 3, y1 - 3)):
            assert difference.getpixel((x, y)) < 5
        assert difference.getpixel(((x0 + x1) // 2, (y0 + y1) // 2 + 5)) > 30
    asyncio.run(scenario())
