import asyncio
import io
import shutil
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import color_adjustments
from app.export_progress import progress_details
from app.main import create_app
from app.models import Settings
from app.service import Recorder


def add_photo(rec, index=1):
    image = Image.new('RGB', (320, 240), (80, 110, 140))
    ImageDraw.Draw(image).rectangle((160, 0, 319, 239), fill=(160, 140, 120))
    path = rec.root / 'frames' / f'{index:032x}.jpg'
    image.save(path, quality=95)
    with rec.store.connect() as db:
        db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)', (path.stem, index, path.stat().st_size))
    return path


def test_neutral_adjustment_is_identity_and_brightness_and_contrast_have_expected_effects():
    assert color_adjustments.adjustment_lut() == list(range(256)) * 3
    brighter = color_adjustments.adjustment_lut(20, 100)
    assert [brighter[v] for v in (0, 80, 140, 255)] == [51, 131, 191, 255]
    contrast = color_adjustments.adjustment_lut(0, 150)
    assert [contrast[v] for v in (0, 64, 128, 192, 255)] == [0, 32, 128, 224, 255]
    assert not color_adjustments.enabled({})
    assert color_adjustments.enabled({'contrast': 120})


def test_color_preview_uses_shared_adjustment_preserves_original_and_is_project_scoped(tmp_path):
    with TestClient(create_app(tmp_path, demo=True)) as client:
        rec = client.app.state.recorder
        path = add_photo(rec)
        before = path.read_bytes()
        original = client.get(f'/media/previews/{path.stem}.jpg').content
        response = client.post('/api/color-preview', json={'frame_id': path.stem, 'brightness': 10, 'contrast': 120})
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/png'
        assert response.content == color_adjustments.preview(original, 10, 120)
        assert path.read_bytes() == before
        assert client.get(f'/media/previews/{path.stem}.jpg').content == original
        assert not list((tmp_path / 'exports').iterdir())
        other = client.post('/api/projects', json={'name': 'Other'}).json()['id']
        assert client.post(f'/api/projects/{other}/color-preview', json={'frame_id': path.stem}).status_code == 404
        assert client.post('/api/color-preview', json={'frame_id': '../frames/x'}).status_code == 422
        for values in ({'brightness': -101}, {'brightness': 101}, {'brightness': 2.5}, {'contrast': -1}, {'contrast': 201}, {'contrast': '120'}):
            assert client.post('/api/color-preview', json={'frame_id': path.stem, **values}).status_code == 422
            assert client.post('/api/exports', json=values).status_code == 422


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('normalize', [False, True])
def test_color_export_matches_preview_and_survives_restart(tmp_path, normalize):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240))
        paths = [add_photo(rec, i) for i in (1, 2)]
        originals = [p.read_bytes() for p in paths]
        preview = await rec.preview_frame(paths[0].stem, dict(brightness=10, contrast=120))
        async def queued(self):
            pass
        with patch.object(Recorder, 'run_exports', queued):
            job = await rec.create_export(brightness=10, contrast=120, normalize_lighting=normalize,
                                          interpolation='repeat', intermediate_frames=2)
            await rec.export_task
        resumed = Recorder(tmp_path, True)
        await resumed.start()
        try:
            await resumed.export_task
        finally:
            await resumed.stop()
        saved = rec.store.rows('SELECT * FROM exports')[0]
        assert saved['status'] == 'complete', saved['error']
        assert saved['brightness'] == 10 and saved['contrast'] == 120
        path = tmp_path / 'exports' / f"{job['id']}.mp4"
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        stride = 320 * 240 * 3
        assert len(raw) == stride * 4
        with Image.open(io.BytesIO(preview)) as expected:
            for n in range(4):
                actual = Image.frombytes('RGB', (320, 240), raw[n * stride:(n + 1) * stride])
                for point in ((80, 120), (240, 120)):
                    assert actual.getpixel(point) == pytest.approx(expected.getpixel(point), abs=4)
        assert [p.read_bytes() for p in paths] == originals
        assert list((tmp_path / 'exports').iterdir()) == [path]
    asyncio.run(scenario())


def test_failed_adjustments_remove_temporary_files(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        original = add_photo(rec)
        def fail(paths, directory, *args):
            (directory / 'partial.grade.png').write_bytes(b'partial')
            raise InterruptedError('stopping')
        with patch('app.service.color_adjustments.prepare_frames', fail):
            await rec.create_export(contrast=125)
            await rec.export_task
        saved = rec.store.rows('SELECT * FROM exports')[0]
        assert saved['status'] == 'queued' and saved['progress'] is None
        assert original.exists()
        assert not list((tmp_path / 'exports').iterdir())
    asyncio.run(scenario())


def test_saved_values_and_legacy_defaults_available_in_api(tmp_path):
    async def queued(self):
        pass
    with TestClient(create_app(tmp_path, demo=True)) as client, patch.object(Recorder, 'run_exports', queued):
        add_photo(client.app.state.recorder)
        result = client.post('/api/exports', json={'brightness': -15, 'contrast': 140}).json()
        assert result['brightness'] == -15 and result['contrast'] == 140
        saved = client.get('/api/exports').json()[0]
        assert saved['brightness'] == -15 and saved['contrast'] == 140
        assert client.post('/api/exports', json={}).json()['contrast'] == 100
        with client.app.state.recorder.store.connect() as db:
            db.execute("INSERT INTO exports(id,created_at,status,frames,fps) VALUES ('legacy',0,'complete',1,24)")
        legacy = next(j for j in client.get('/api/exports').json() if j['id'] == 'legacy')
        assert legacy['brightness'] == 0 and legacy['contrast'] == 100


@pytest.mark.skipif(not shutil.which('node'), reason='Node required for UI logic checks')
def test_color_preview_ignores_stale_slider_responses_and_keeps_reference_frame():
    subprocess.run(['node', '-e', r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, Object.assign(new EventTarget(), {
    value: '', textContent: '', removeAttribute(name) {delete this[name];}
  }));
  return nodes.get(id);
};
node('export-brightness').value = '0';
node('export-contrast').value = '100';
const timers = new Set(), requests = [], revoked = [], saved = [];
const context = vm.createContext({
  $: node, Event, AbortController, tab: 'videos', currentId: 'project-a',
  timeline: {id: 'project-a'}, currentFrame: {id: 'photo-a', captured_at: 10},
  endpoint: (id, path) => `${id}/${path}`, media: (...parts) => parts.join('/'), date: String,
  setTimeout: fn => {timers.add(fn); return fn;}, clearTimeout: fn => timers.delete(fn),
  URL: {createObjectURL: blob => `blob:${blob}`, revokeObjectURL: url => revoked.push(url)},
  api: (path, method, body, signal) => new Promise(resolve => requests.push({path, body, signal, resolve})),
  localStorage: {setItem: (key, value) => saved.push(JSON.parse(value))},
});
vm.runInContext(source.slice(source.indexOf('let colorPreviewFrame ='), source.indexOf('let cinematicAnalysisKey =')), context);
context.updateExportEstimate = () => {context.updateColorPreview(); return true;};
context.videoExportOptions = () => ({brightness: +node('export-brightness').value, contrast: +node('export-contrast').value});
context.updateVideoButtons = () => context.updateColorPreview();
const listenersStart = source.indexOf('for (const id of ["smooth-motion"');
vm.runInContext(source.slice(listenersStart, source.indexOf('let timeline = null', listenersStart)), context);
const runTimers = () => {for (const fn of [...timers]) {timers.delete(fn); fn();}};
const flush = () => new Promise(setImmediate);
(async () => {
  node('color-use-frame').onclick(); runTimers();
  assert.equal(requests[0].body.frame_id, 'photo-a');
  node('export-brightness').value = '15';
  node('export-brightness').dispatchEvent(new Event('input')); runTimers();
  assert.equal(requests[0].signal.aborted, true);
  requests[1].resolve('new'); await flush();
  requests[0].resolve('old'); await flush();
  assert.equal(node('color-adjusted').src, 'blob:new');
  assert.equal(node('color-preview-pair').hidden, false);
  assert.equal(saved.at(-1).brightness, 15);
  context.currentFrame = {id: 'photo-b', captured_at: 20};
  node('export-contrast').value = '130';
  node('export-contrast').dispatchEvent(new Event('input')); runTimers();
  assert.equal(requests[2].body.frame_id, 'photo-a'); // Keep the comparison fixed while browsing.
  node('color-use-frame').onclick(); runTimers();
  assert.equal(requests[3].body.frame_id, 'photo-b');
  assert.equal(requests[2].signal.aborted, true);
  assert(revoked.includes('blob:new'));
  requests[3].resolve('frame-b'); await flush();
  node('color-reset').onclick(); runTimers();
  assert.equal(requests[4].body.brightness, 0);
  assert.equal(requests[4].body.contrast, 100);
  assert.equal(saved.at(-1).contrast, 100);
  context.clearColorPreview();
  context.currentId = 'project-b';
  requests[4].resolve('wrong-project'); await flush();
  assert.equal(node('color-preview-pair').hidden, true);
  assert.equal(node('color-adjusted').src, undefined);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''], check=True, capture_output=True, text=True)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
def test_manual_adjustments_work_with_cinematic_zoom_and_leave_timer_colors_intact(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240))
        for index in (1, 2):
            add_photo(rec, index)
        stages = []
        from app.export_progress import ExportProgress
        original_report = ExportProgress.report
        def report(self, stage, completed, total):
            stages.append(stage)
            original_report(self, stage, completed, total)
            saved = rec.store.rows('SELECT * FROM exports')[0]
            if saved['progress']:
                assert progress_details(saved)['stage_count'] == 9
        with patch.object(ExportProgress, 'report', report):
            job = await rec.create_export(contrast=0, cinematic_focus=True, timing_overlay=True,
                                          interpolation='repeat', intermediate_frames=2)
            await rec.export_task
        saved = rec.store.rows('SELECT * FROM exports')[0]
        assert saved['status'] == 'complete', saved['error']
        assert stages.index('focusing') < stages.index('grading') < stages.index('overlay') < stages.index('encoding')
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(tmp_path / 'exports' / f"{job['id']}.mp4"),
                                       '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        assert len(raw) == 320 * 240 * 3 * 4
        frame = Image.frombytes('RGB', (320, 240), raw[-320 * 240 * 3:])
        assert frame.getpixel((280, 200)) == pytest.approx((128, 128, 128), abs=4)
        assert any(max(pixel) - min(pixel) > 40 for pixel in (frame.getpixel((x, y)) for y in range(150) for x in range(150)))
    asyncio.run(scenario())
