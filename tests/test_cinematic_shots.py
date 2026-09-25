import asyncio
import random
import shutil
import subprocess
from unittest.mock import patch

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat
from fastapi.testclient import TestClient

from app import cinematic
from app.main import create_app
from app.models import Settings
from app.service import Recorder
from app.video import export_filters, output_frame_count


def textured_scene(seed):
    rng = random.Random(seed)
    image = Image.new('RGB', (320, 240))
    draw = ImageDraw.Draw(image)
    for y in range(0, 240, 20):
        for x in range(0, 320, 20):
            value = rng.randrange(40, 190)
            draw.rectangle((x, y, x + 19, y + 19), fill=(value,) * 3)
    return image


def test_detects_broad_angle_change_but_not_exposure_or_local_growth(tmp_path):
    original = textured_scene(1)
    brighter = original.point(lambda value: round(value * 1.1 + 15))
    growth = brighter.copy()
    ImageDraw.Draw(growth).rectangle((140, 140, 165, 200), fill=(20, 80, 20))
    angle = textured_scene(2)
    paths = []
    for i, image in enumerate((original, brighter, growth, angle, angle)):
        path = tmp_path / f'{i}.png'
        image.save(path)
        paths.append(path)
    updates = []
    assert cinematic.scene_ranges(paths, lambda: None, lambda *args: updates.append(args)) == [(0, 3), (3, 5)]
    assert updates[-1] == ('scene_analysis', 5, 5)
    shots = cinematic.prepare_frames(paths, lambda: None, scenes=[(0, 3), (3, 5)])
    assert ImageChops.difference(Image.open(paths[3]), angle).getbbox() is None


def test_aspect_change_and_cancellation(tmp_path):
    paths = [tmp_path / 'a.png', tmp_path / 'b.png']
    Image.new('RGB', (320, 240), 'gray').save(paths[0])
    Image.new('RGB', (240, 320), 'gray').save(paths[1])
    assert cinematic.scene_ranges(paths, lambda: None) == [(0, 1), (1, 2)]
    def stop():
        raise InterruptedError('stopping')
    with pytest.raises(InterruptedError):
        cinematic.scene_ranges(paths, stop)


def test_zoom_api_validation_persistence_and_legacy_default(tmp_path):
    app = create_app(tmp_path, demo=True)
    async def queued(self):
        pass
    with TestClient(app) as client, patch.object(Recorder, 'run_exports', queued):
        client.post('/api/capture', json={})
        for invalid in (-1, 101, 12.5, '20', True):
            assert client.post('/api/exports', json={'cinematic_zoom_percent': invalid}).status_code == 422
        for amount in (0, 35, 100):
            response = client.post('/api/exports', json={'cinematic_focus': True, 'cinematic_zoom_percent': amount})
            assert response.status_code == 202
            assert response.json()['cinematic_zoom_percent'] == amount
            assert client.get('/api/exports').json()[0]['cinematic_zoom_percent'] == amount
        with app.state.recorder.store.connect() as db:
            db.execute("INSERT INTO exports(id,created_at,status,frames,fps) VALUES ('legacy',0,'complete',1,24)")
        assert next(job for job in client.get('/api/exports').json() if job['id'] == 'legacy')['cinematic_zoom_percent'] == 20


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('mode', ['none', 'repeat', 'blend', 'motion'])
@pytest.mark.parametrize('lengths', [(2, 3), (1, 1, 1)])
def test_shot_reset_preserves_duration_and_does_not_blend_across_cuts(tmp_path, mode, lengths):
    colors = [(20, 40, 70), (70, 40, 20), (40, 70, 20)]
    shots, index = [], 0
    for length, color in zip(lengths, colors):
        shots.append(dict(start=index, end=index + length))
        for _ in range(length):
            image = Image.new('RGB', (320, 240), color)
            ImageDraw.Draw(image).rectangle((140, 90, 180, 130), fill=(230,) * 3)
            image.save(tmp_path / f'{index}.png')
            index += 1
    job = dict(frames=index, fps=30, interpolation=mode, intermediate_frames=3,
               cinematic_focus=True, cinematic_zoom_percent=40)
    factor = 1 if mode == 'none' else 4
    raw = subprocess.check_output([
        'ffmpeg', '-v', 'error', '-framerate', f'30/{factor}', '-i', str(tmp_path / '%d.png'),
        '-vf', export_filters(job, Settings(width=320, height=240), shots=shots),
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-threads', '1', '-'])
    stride = 320 * 240 * 3
    frames = [Image.frombytes('RGB', (320, 240), raw[n:n + stride]) for n in range(0, len(raw), stride)]
    assert len(frames) == output_frame_count(index, mode, 3)
    for i, (shot, color) in enumerate(zip(shots, colors)):
        first = shot['start'] * factor
        end = shot['end'] * factor if i < len(shots) - 1 else len(frames)
        for frame in frames[first:end]:
            assert frame.getpixel((10, 10)) == pytest.approx(color, abs=3)
        def width(frame):
            box = frame.convert('L').point(lambda p: 255 if p > 180 else 0).getbbox()
            return box[2] - box[0]
        assert width(frames[first]) == 41  # Starts over at full view on every cut.
        if end - first > 1:
            assert width(frames[end - 1]) / 41 == pytest.approx(1.4, abs=.04)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('zoom', [0, 10, 100])
def test_configured_zoom_changes_actual_rendered_scale(tmp_path, zoom):
    image = Image.new('RGB', (320, 240), (30,) * 3)
    ImageDraw.Draw(image).rectangle((140, 100, 180, 140), fill=(230,) * 3)
    for i in range(2):
        image.save(tmp_path / f'{i}.png')
    job = dict(frames=2, fps=30, interpolation='repeat', intermediate_frames=5,
               cinematic_focus=True, cinematic_zoom_percent=zoom)
    raw = subprocess.check_output([
        'ffmpeg', '-v', 'error', '-framerate', '30/6', '-i', str(tmp_path / '%d.png'),
        '-vf', export_filters(job, Settings(width=320, height=240)),
        '-f', 'rawvideo', '-pix_fmt', 'gray', '-threads', '1', '-'])
    frame = Image.frombytes('L', (320, 240), raw[-320 * 240:])
    box = frame.point(lambda p: 255 if p > 180 else 0).getbbox()
    assert (box[2] - box[0]) / 41 == pytest.approx(1 + zoom / 100, abs=.04)


def test_lighting_uses_separate_references_for_each_shot(tmp_path):
    from app import lighting
    paths = []
    colors = [(60, 90, 100)] * 2 + [(170, 100, 70)] * 2
    for i, color in enumerate(colors):
        path = tmp_path / f'{i}.png'
        Image.new('RGB', (320, 240), color).save(path)
        paths.append(path)
    output = tmp_path / 'normalized'
    output.mkdir()
    lighting.prepare_frames(paths, output, lambda: None, scenes=[(0, 2), (2, 4)])
    for i, color in enumerate(colors):
        with Image.open(output / f'{i}.png') as image:
            assert image.getpixel((100, 100)) == color


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('mode', ['repeat', 'blend', 'motion'])
@pytest.mark.parametrize('threshold', [55, 100])
def test_real_export_detects_cuts_resets_zoom_and_retains_saved_amount_after_restart(tmp_path, mode, threshold):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240, export_fps=30))
        originals = []
        for i in range(4):
            image = textured_scene(1 if i < 2 else 2)
            ImageDraw.Draw(image).rectangle((140, 100, 180, 140), fill=(240,) * 3)
            path = rec.root / 'frames' / f'{i + 1:032x}.jpg'
            image.save(path, quality=95)
            originals.append((path, path.read_bytes()))
            with rec.store.connect() as db:
                db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)',
                           (path.stem, 1 + i * 86400, path.stat().st_size))
        async def queued(self):
            pass
        with patch.object(Recorder, 'run_exports', queued):
            job = await rec.create_export(cinematic_focus=True, cinematic_zoom_percent=35, cinematic_reset_threshold=threshold,
                                          interpolation=mode, intermediate_frames=2, timing_overlay=True)
            await rec.export_task
        resumed = Recorder(tmp_path, True)
        await resumed.start()
        try:
            await resumed.export_task
        finally:
            await resumed.stop()
        saved = rec.store.rows('SELECT * FROM exports')[0]
        assert saved['status'] == 'complete', saved['error']
        assert saved['cinematic_zoom_percent'] == 35
        assert saved['cinematic_reset_threshold'] == threshold
        video = tmp_path / 'exports' / f"{job['id']}.mp4"
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video),
                                       '-f', 'rawvideo', '-pix_fmt', 'gray', '-'])
        stride = 320 * 240
        assert len(raw) == 10 * stride
        widths = []
        for n in (0, 5, 6, 9):
            frame = Image.frombytes('L', (320, 240), raw[n * stride:(n + 1) * stride])
            box = frame.crop((60, 70, 260, 200)).point(lambda p: 255 if p > 215 else 0).getbbox()
            widths.append(box[2] - box[0])
        assert widths[0] == 41
        if threshold == 55:
            assert widths[2] == 41
            assert widths[1] / 41 == pytest.approx(1.35, abs=.04)
        else:
            assert widths[2] > 45  # High threshold suppresses the cut; zoom continues.
        assert widths[3] / 41 == pytest.approx(1.35, abs=.04)
        assert all(path.read_bytes() == before for path, before in originals)
        assert list((tmp_path / 'exports').iterdir()) == [video]
    asyncio.run(scenario())


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_orientation_cut_keeps_all_frames_and_each_shot_inside_its_own_bounds(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240, export_fps=30))
        for i in range(4):
            image = Image.new('RGB', (320, 240) if i < 2 else (240, 320), (80, 100, 120))
            path = rec.root / 'frames' / f'{i + 1:032x}.jpg'
            image.save(path)
            with rec.store.connect() as db:
                db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)',
                           (path.stem, i, path.stat().st_size))
        job = await rec.create_export(cinematic_focus=True, interpolation='repeat', intermediate_frames=2)
        await rec.export_task
        saved = rec.store.rows('SELECT * FROM exports')[0]
        assert saved['status'] == 'complete', saved['error']
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(tmp_path / 'exports' / f"{job['id']}.mp4"),
                                       '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        stride = 320 * 240 * 3
        assert len(raw) == 10 * stride
        for n in range(10):
            image = Image.frombytes('RGB', (320, 240), raw[n * stride:(n + 1) * stride])
            assert image.getpixel((160, 120)) == pytest.approx((80, 100, 120), abs=4)
            assert image.getpixel((10, 120)) == pytest.approx((80, 100, 120) if n < 6 else (0, 0, 0), abs=4)
    asyncio.run(scenario())


def sparse_scene(view, growth=0):
    """A container against a plain wall, seen from three camera positions."""
    image = Image.new('RGB', (320, 240), (220, 216, 206))
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = [(45, 35, 275, 225), (90, 90, 230, 222), (105, 115, 217, 227)][view]
    draw.rectangle((left, top, right, bottom), fill=(90, 80, 60), outline=(180, 180, 170), width=4)
    for i in range(9):
        x = left + 8 + i * (right - left - 16) // 8
        draw.line((x, bottom - 15, x + 6, top + 18 - growth), fill=(75, 115, 35), width=2)
    draw.line((left, top + 40, right, top + 40), fill=(230, 230, 210), width=3)
    return image


def test_detects_both_reframings_against_plain_wall(tmp_path):
    paths = []
    for i, (view, growth) in enumerate([(0, 0), (0, 2), (1, 0), (1, 2), (2, 0), (2, 2)]):
        path = tmp_path / f'{i}.png'
        sparse_scene(view, growth).save(path)
        paths.append(path)
    # Neither cut meets the former "most of the image changed" requirement.
    assert cinematic.scene_ranges(paths, lambda: None) == [(0, 2), (2, 4), (4, 6)]
    # Detection also works when exporting a short range around the second cut.
    assert cinematic.scene_ranges(paths[3:5], lambda: None) == [(0, 1), (1, 2)]


def test_plain_wall_exposure_shadow_and_local_growth_do_not_reset_camera(tmp_path):
    original = sparse_scene(1)
    brighter = original.point(lambda value: round(value * .85 + 30))
    shadow = original.copy()
    pixels = shadow.load()
    for y in range(shadow.height):
        for x in range(shadow.width):
            pixels[x, y] = tuple(round(value * (.65 + .35 * x / shadow.width)) for value in pixels[x, y])
    local_change = sparse_scene(1, growth=8)
    ImageDraw.Draw(local_change).ellipse((140, 60, 150, 90), fill=(40, 110, 20))
    paths = []
    for i, image in enumerate((original, brighter, original, shadow, original, local_change)):
        path = tmp_path / f'{i}.png'
        image.save(path)
        paths.append(path)
    assert cinematic.scene_ranges(paths, lambda: None) == [(0, 6)]
