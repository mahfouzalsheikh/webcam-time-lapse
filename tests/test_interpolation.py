import asyncio
import json
import shutil
import sqlite3
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import create_app
from app.models import Settings
from app.service import Recorder


def seed(rec, count):
    originals = {}
    for i in range(count):
        path = rec.root / 'frames' / f'{i + 1:032x}.jpg'
        image = Image.new('RGB', (320, 240), (25, 30, 35))
        ImageDraw.Draw(image).rectangle((30 + 20 * i, 80, 70 + 20 * i, 120), fill=(220, 70, 50))
        image.save(path, quality=95)
        originals[path] = path.read_bytes()
        with rec.store.connect() as db:
            db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)',
                       (path.stem, i + 1, path.stat().st_size))
    return originals


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('mode,count,extra,fps', [
    ('none', 3, 5, 24), ('blend', 1, 5, 30), ('motion', 1, 5, 60),
    ('blend', 2, 1, 24), ('motion', 2, 5, 30), ('blend', 5, 5, 60),
    ('motion', 5, 2, 24), ('blend', 2, 59, 1), ('motion', 2, 59, 60),
    ('repeat', 1, 5, 30), ('repeat', 5, 5, 24), ('repeat', 2, 59, 1),
    ('repeat', 2, 59, 60),
])
def test_real_export_matches_estimate_and_keeps_endpoints(tmp_path, mode, count, extra, fps):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240))
        originals = seed(rec, count)
        # Excluded and post-cutoff photos must not add video frames.
        with rec.store.connect() as db:
            if count > 2:
                db.execute('UPDATE frames SET excluded=1 WHERE captured_at=2')
        cutoff = count - 1 if count > 2 else count
        included = count - 2 if count > 2 else count
        expected = included + max(0, included - 1) * (extra if mode != 'none' else 0)
        job = await rec.create_export(cutoff=cutoff, interpolation=mode, intermediate_frames=extra, fps=fps)
        assert job['output_frames'] == expected
        assert job['duration_seconds'] == expected / fps
        await rec.export_task
        result = rec.store.rows('SELECT * FROM exports')[0]
        assert result['status'] == 'complete', result['error']
        video = tmp_path / 'exports' / f"{job['id']}.mp4"
        probe = json.loads(subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(video)]))['streams'][0]
        assert int(probe['nb_frames']) == expected
        assert probe['r_frame_rate'] == f'{fps}/1'
        assert float(probe['duration']) == pytest.approx(expected / fps, abs=.001)
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video), '-f', 'rawvideo',
                                       '-pix_fmt', 'rgb24', '-threads', '1', '-'])
        stride = 320 * 240 * 3
        first = Image.frombytes('RGB', (320, 240), raw[:stride])
        last = Image.frombytes('RGB', (320, 240), raw[-stride:])
        assert first.getpixel((40, 90))[0] > 180
        last_index = cutoff - 1
        assert last.getpixel((40 + 20 * last_index, 90))[0] > 180
        if included > 1 and mode in ('blend', 'motion'):
            middle = Image.frombytes('RGB', (320, 240), raw[(extra // 2 + 1) * stride:(extra // 2 + 2) * stride])
            assert middle.tobytes() != first.tobytes()
            assert middle.tobytes() != last.tobytes()
        assert {path: path.read_bytes() for path in originals} == originals
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['motion', 'repeat'])
def test_api_validation_estimates_and_project_isolation(tmp_path, mode):
    app = create_app(tmp_path, demo=True)
    async def queued(self):
        pass
    with TestClient(app) as client, patch.object(Recorder, 'run_exports', queued):
        seed(app.state.recorder, 24)
        for payload in [{'interpolation': 'ai'}, {'intermediate_frames': 0},
                        {'intermediate_frames': 60}, {'intermediate_frames': 1.5},
                        {'fps': 0}, {'fps': 61}, {'fps': 29.5}, {'resolution': '8k'}]:
            assert client.post('/api/exports', json=payload).status_code == 422
        response = client.post('/api/exports', json={'interpolation': mode, 'intermediate_frames': 5, 'fps': 60})
        assert response.status_code == 202
        job = response.json()
        assert job['output_frames'] == 139
        assert job['duration_seconds'] == pytest.approx(139 / 60)
        listed = client.get('/api/exports').json()[0]
        assert listed['interpolation'] == mode and listed['intermediate_frames'] == 5
        assert listed['output_frames'] == 139
        other = client.post('/api/projects', json={'name': 'Other'}).json()['id']
        assert client.get(f'/api/projects/{other}/exports').json() == []


def test_old_exports_get_no_interpolation_on_upgrade(tmp_path):
    with sqlite3.connect(tmp_path / 'state.sqlite3') as db:
        db.execute('CREATE TABLE exports (id TEXT PRIMARY KEY,created_at REAL,status TEXT,frames INTEGER,fps INTEGER,error TEXT)')
        db.execute("INSERT INTO exports VALUES ('old',1,'complete',24,24,NULL)")
    with TestClient(create_app(tmp_path, demo=True)) as client:
        job = client.get('/api/exports').json()[0]
        assert job['interpolation'] == 'none' and job['intermediate_frames'] == 0
        assert job['output_frames'] == 24 and job['duration_seconds'] == 1


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_export_resolution_applies_only_to_video_and_survives_restart(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=3840, height=2160, export_fps=60))
        originals = seed(rec, 2)
        async def queued(self):
            pass
        with patch.object(Recorder, 'run_exports', queued):
            job = await rec.create_export(resolution='1080p', fps=30, interpolation='blend',
                                          intermediate_frames=2, normalize_lighting=True)
            await rec.export_task
        assert (job['width'], job['height']) == (1920, 1080)
        assert 'settings' not in job
        assert job['output_frames'] == 4
        assert (rec.settings().width, rec.settings().height, rec.settings().export_fps) == (3840, 2160, 60)
        # The queued snapshot keeps its export resolution even if settings change.
        await rec.save_settings(Settings(width=1280, height=720))
        resumed = Recorder(tmp_path, True)
        await resumed.start()
        try:
            await resumed.export_task
        finally:
            await resumed.stop()
        result = rec.store.rows('SELECT * FROM exports')[0]
        assert result['status'] == 'complete', result['error']
        video = tmp_path / 'exports' / f"{job['id']}.mp4"
        probe = json.loads(subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(video)]))['streams'][0]
        assert (probe['width'], probe['height']) == (1920, 1080)
        assert int(probe['nb_frames']) == 4
        assert probe['r_frame_rate'] == '30/1'
        assert {path: path.read_bytes() for path in originals} == originals
    asyncio.run(scenario())
    with TestClient(create_app(tmp_path, demo=True)) as client:
        listed = client.get('/api/exports').json()[0]
        assert (listed['width'], listed['height']) == (1920, 1080)
        assert 'settings' not in listed


def filtered_frames(paths, mode, extra=3):
    from app.video import export_filters

    job = dict(frames=len(paths), fps=24, interpolation=mode, intermediate_frames=extra)
    manifest = paths[0].parent / 'input.txt'
    manifest.write_text(''.join(f"file '{path}'\n" for path in paths))
    raw = subprocess.check_output([
        'ffmpeg', '-v', 'error', '-r', f'24/{extra + 1}', '-f', 'concat', '-safe', '0',
        '-i', str(manifest), '-vf', export_filters(job, Settings(width=320, height=240)),
        '-threads', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
    stride = 320 * 240 * 3
    return [Image.frombytes('RGB', (320, 240), raw[i:i + stride])
            for i in range(0, len(raw), stride)]


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_motion_preserves_background_and_sharp_edges_including_first_gap(tmp_path):
    from PIL import ImageChops, ImageStat

    paths = []
    for i in range(5):
        frame = Image.new('RGB', (320, 240), (70, 80, 90))
        draw = ImageDraw.Draw(frame)
        for x in range(0, 320, 12):
            draw.line((x, 0, x, 239), fill=(130, 140, 150), width=2)
        draw.rectangle((60 + i * 8, 90, 91 + i * 8, 137), fill=(210, 210, 210))
        for y in range(95, 137, 8):
            draw.line((64 + i * 8, y, 87 + i * 8, y), fill=(25, 25, 25), width=2)
        path = tmp_path / f'{i}.png'
        frame.save(path)
        paths.append(path)
    motion = filtered_frames(paths, 'motion')
    blend = filtered_frames(paths, 'blend')
    assert len(motion) == len(blend) == 17
    for index in (2, 6, 10, 14):
        x = 62 + (index // 4) * 8
        # Halfway through movement, the trailing edge has cleared this pixel.
        # Crossfade leaves a translucent second edge; motion should not.
        assert abs(motion[index].getpixel((x, 100))[0] - 70) < 8
        assert blend[index].getpixel((x, 100))[0] > 115
        background = motion[index].crop((0, 0, 320, 70))
        assert ImageStat.Stat(ImageChops.difference(background, motion[0].crop((0, 0, 320, 70)))).mean == [0, 0, 0]
    for i, path in enumerate(paths):
        with Image.open(path) as original:
            error = ImageStat.Stat(ImageChops.difference(motion[i * 4], original)).mean
            assert max(error) < 2


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_motion_does_not_morph_across_scene_cuts(tmp_path):
    from PIL import ImageStat

    paths = []
    for i, value in enumerate((35, 35, 210, 210)):
        path = tmp_path / f'{i}.png'
        Image.new('RGB', (320, 240), (value,) * 3).save(path)
        paths.append(path)
    frames = filtered_frames(paths, 'motion')
    assert len(frames) == 13
    for frame in frames:
        mean = ImageStat.Stat(frame).mean[0]
        assert min(abs(mean - 35), abs(mean - 210)) < 3


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_repeat_lengthens_video_with_exact_copies_of_each_photo(tmp_path):
    paths = []
    for i in range(3):
        frame = Image.new('RGB', (320, 240), (60, 80, 100))
        draw = ImageDraw.Draw(frame)
        for x in range(20 + i * 20, 60 + i * 20, 2):
            draw.line((x, 80, x, 160), fill=(210, 190, 170))
        path = tmp_path / f'{i}.png'
        frame.save(path)
        paths.append(path)
    frames = filtered_frames(paths, 'repeat', extra=5)
    assert len(frames) == 13
    for index, frame in enumerate(frames):
        source_index = min(index // 6, 2)
        assert frame.tobytes() == frames[source_index * 6].tobytes()
    assert len({frame.tobytes() for frame in frames}) == 3
