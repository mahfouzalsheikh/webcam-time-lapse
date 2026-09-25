import asyncio
import shutil
import subprocess
import threading
import time
from statistics import pstdev
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageStat

from app.lighting import prepare_frames
from app import lighting
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
    prepare_frames(paths, output, lambda: None)
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
        def fail(paths, directory, check, progress=None):
            (directory / 'partial.png').write_bytes(b'partial')
            if progress:
                progress('normalizing', 0, len(paths))
            raise failure
        with patch('app.service.lighting.prepare_frames', side_effect=fail):
            job = await rec.create_export(normalize_lighting=True)
            await rec.export_task
        assert rec.store.rows('SELECT status FROM exports')[0]['status'] == status
        assert rec.store.rows('SELECT progress FROM exports')[0]['progress'] is None
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


def test_parallel_correction_is_bounded_and_matches_serial_pixels(tmp_path):
    rec = Recorder(tmp_path, True)
    paths = [add_frame(rec, i, (70 + i * 9, 100, 120)) for i in range(12)]
    serial, parallel = tmp_path / 'serial', tmp_path / 'parallel'
    serial.mkdir()
    parallel.mkdir()
    with patch('app.lighting.os.cpu_count', return_value=1):
        prepare_frames(paths, serial, lambda: None)
    lock = threading.Lock()
    active = peak = 0
    correct = lighting.correct_frame
    def tracked(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.02)
            return correct(*args)
        finally:
            with lock:
                active -= 1
    updates = []
    with patch('app.lighting.os.cpu_count', return_value=16), patch('app.lighting.correct_frame', tracked):
        prepare_frames(paths, parallel, lambda: None,
                       lambda *update: updates.append(update))
    assert 1 < peak <= 4
    assert active == 0
    assert [n for stage, n, _ in updates if stage == 'normalizing'] == list(range(13))
    for path in paths:
        assert (serial / f'{path.stem}.png').read_bytes() == (parallel / f'{path.stem}.png').read_bytes()


def test_interrupt_waits_for_correction_workers_before_cleanup(tmp_path):
    rec = Recorder(tmp_path, True)
    paths = [add_frame(rec, i, (100, 120, 130)) for i in range(12)]
    output = tmp_path / 'corrected'
    output.mkdir()
    started = threading.Event()
    active = set()
    lock = threading.Lock()
    def slow_writer(path, *args):
        with lock:
            active.add(path)
        started.set()
        try:
            time.sleep(.1)
            (output / f'{path.stem}.png').touch()
        finally:
            with lock:
                active.remove(path)
    def check():
        if started.is_set():
            raise InterruptedError('stopping')
    with patch('app.lighting.correct_frame', slow_writer):
        with pytest.raises(InterruptedError):
            prepare_frames(paths, output, check)
    assert not active
    assert 1 <= len(list(output.iterdir())) <= 4


def test_growing_subject_does_not_make_background_pump(tmp_path):
    from PIL import ImageDraw

    paths = []
    for i, gains in enumerate([(1., 1., 1.), (.7, .85, .95), (1.2, 1.1, .9), (1., 1., 1.)]):
        frame = Image.new('RGB', (480, 360), (120, 130, 140))
        draw = ImageDraw.Draw(frame)
        # Detailed, stationary background and a subject that grows substantially.
        for x in range(0, 480, 16):
            draw.line((x, 0, x, 359), fill=(75, 85, 95), width=2)
        draw.rectangle((170 - i * 15, 100 - i * 10, 230 + i * 35, 230 + i * 20),
                       fill=(35, 150, 45))
        frame = frame.point([round(v * gain) for gain in gains for v in range(256)])
        path = tmp_path / f'{i}.png'
        frame.save(path)
        paths.append(path)
    output = tmp_path / 'corrected'
    output.mkdir()
    prepare_frames(paths, output, lambda: None)
    backgrounds, subjects = [], []
    for path in paths:
        with Image.open(output / path.name) as result:
            backgrounds.append(result.getpixel((40, 40)))
            subjects.append(result.getpixel((200, 150)))
            # No spatial or temporal averaging: the two-pixel stripe stays sharp.
            assert result.getpixel((32, 40))[0] < result.getpixel((34, 40))[0] - 35
    for colors in (backgrounds, subjects):
        assert max(pstdev(channel) for channel in zip(*colors)) < 1.5


def test_identity_correction_preserves_full_resolution_and_detail(tmp_path):
    from PIL import ImageDraw

    path = tmp_path / 'detail.png'
    frame = Image.new('RGB', (641, 479), (100, 120, 140))
    draw = ImageDraw.Draw(frame)
    for x in range(0, 641, 2):
        draw.line((x, 0, x, 478), fill=(30, 60, 80))
    frame.save(path)
    output = tmp_path / 'corrected'
    output.mkdir()
    prepare_frames([path], output, lambda: None)
    with Image.open(output / path.name) as corrected:
        assert corrected.size == frame.size
        assert corrected.tobytes() == frame.tobytes()


def test_highlight_correction_keeps_tonal_detail():
    lut = lighting.correction_lut([2., 1., .5])
    assert lut[128] < lut[160] < lut[192] < lut[224] < lut[255]
    assert lut[256:512] == list(range(256))
    assert all(0 <= value <= 255 for value in lut)
