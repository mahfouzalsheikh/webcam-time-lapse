import asyncio
import json
import shutil
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.export_progress import ExportProgress, FFmpegProgress, progress_details
from app.main import create_app
from app.models import Settings
from app.service import Recorder


def progress_job(rec, status='running'):
    with rec.store.connect() as db:
        db.execute('INSERT INTO exports(id,created_at,status,frames,fps,normalize_lighting) VALUES (?,?,?,?,?,1)',
                   ('a' * 32, time.time(), status, 100, 24))
    return 'a' * 32


def test_stage_eta_uses_only_current_stage_and_hides_unreliable_estimates():
    state = dict(stage='normalizing', completed=25, total=100, started_at=100,
                 stage_started_at=200, last_advanced_at=245)
    job = dict(status='running', normalize_lighting=1, progress=json.dumps(state))
    result = progress_details(job, now=250)
    assert result['stage_number'] == 2 and result['stage_count'] == 4
    assert result['percent'] == 25
    assert result['elapsed_seconds'] == 150
    assert result['eta_seconds'] == 150
    assert result['units_per_second'] == .5
    assert progress_details(job, now=310)['eta_seconds'] is None
    state.update(stage='encoding', completed=0, total=595, stage_started_at=300, last_advanced_at=300)
    job['progress'] = json.dumps(state)
    assert progress_details(job, now=320)['eta_seconds'] is None
    assert progress_details(job, now=320)['percent'] == 0
    assert progress_details({**job, 'status': 'queued'}, now=320) is None
    assert progress_details({**job, 'status': 'complete'}, now=320) is None
    state.update(stage='finalizing', completed=0, total=0)
    result = progress_details({**job, 'progress': json.dumps(state)}, now=320)
    assert result['percent'] is None and result['eta_seconds'] is None


def test_progress_updates_are_throttled_and_stage_boundaries_are_immediate(tmp_path):
    rec = Recorder(tmp_path, True)
    job_id = progress_job(rec)
    tracker = ExportProgress(rec.store, job_id)
    with patch('app.export_progress.time.monotonic', side_effect=[0, .1, 1.1, 1.2]):
        tracker.report('analyzing', 0, 100)
        tracker.report('analyzing', 1, 100)
        assert json.loads(rec.store.rows('SELECT progress FROM exports')[0]['progress'])['completed'] == 0
        tracker.report('analyzing', 2, 100)
        assert json.loads(rec.store.rows('SELECT progress FROM exports')[0]['progress'])['completed'] == 2
        tracker.report('normalizing', 0, 100)
        saved = json.loads(rec.store.rows('SELECT progress FROM exports')[0]['progress'])
        assert saved['stage'] == 'normalizing' and saved['completed'] == 0


def test_ffmpeg_parser_handles_partial_records_and_waits_for_finalization():
    updates = []
    parser = FFmpegProgress(lambda *args: updates.append(args), 10)
    parser.feed('fra')
    parser.feed('me=3\nfps=0.5\nprogress=cont')
    assert updates == []
    parser.feed('inue\nframe=bad\nframe=-1\nprogress=continue\n')
    assert updates == [('encoding', 3, 10), ('encoding', 3, 10)]
    parser.feed('frame=20\nprogress=continue\n')
    assert updates[-1] == ('finalizing', 0, 0)
    parser.feed('progress=end\n')
    assert updates[-1] == ('finalizing', 0, 0)


def test_running_progress_is_available_via_api_and_refresh(tmp_path):
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        rec = app.state.recorder
        job_id = progress_job(rec)
        tracker = ExportProgress(rec.store, job_id)
        tracker.report('normalizing', 25, 100)
        for _ in range(2):
            result = client.get('/api/exports').json()[0]
            assert result['progress']['completed'] == 25
            assert result['progress']['percent'] == 25
            assert result['progress']['stage'] == 'normalizing'
        other = client.post('/api/projects', json={'name': 'Other progress'}).json()['id']
        assert client.get(f'/api/projects/{other}/exports').json() == []
        with rec.store.connect() as db:
            db.execute("UPDATE exports SET status='failed', progress=NULL")


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg required')
@pytest.mark.parametrize('normalize', [False, True])
def test_real_export_reports_work_done_then_clears_progress(tmp_path, normalize):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.save_settings(Settings(width=320, height=240))
        for i in range(3):
            frame_id = f'{i + 1:032x}'
            path = tmp_path / 'frames' / f'{frame_id}.jpg'
            Image.new('RGB', (320, 240), (80 + i * 20, 100, 110)).save(path)
            with rec.store.connect() as db:
                db.execute('INSERT INTO frames(id,captured_at,bytes) VALUES (?,?,?)', (frame_id, i, path.stat().st_size))
        observed = []
        real_report = ExportProgress.report
        def record(tracker, stage, completed, total):
            real_report(tracker, stage, completed, total)
            observed.append((stage, completed, total))
            if stage == 'normalizing':
                # Other workers may have finished writing before their futures
                # are collected and reported by the coordinating thread.
                assert len(list((tmp_path / 'exports').glob('*.lighting/*.png'))) >= completed
        with patch.object(ExportProgress, 'report', record):
            job = await rec.create_export(normalize_lighting=normalize, interpolation='blend', intermediate_frames=5)
            await rec.export_task
        result = rec.store.rows('SELECT * FROM exports')[0]
        assert result['status'] == 'complete', result['error']
        assert result['progress'] is None
        assert ('encoding', 0, 13) in observed
        assert observed[-1] == ('finalizing', 0, 0)
        if normalize:
            assert ('analyzing', 3, 3) in observed
            assert ('normalizing', 3, 3) in observed
        else:
            assert observed[0] == ('encoding', 0, 13)
        assert (tmp_path / 'exports' / f"{job['id']}.mp4").is_file()
        assert not list((tmp_path / 'exports').glob('*.progress'))
    asyncio.run(scenario())


def test_restart_discards_stale_progress_and_partial_progress_files(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        job_id = progress_job(rec)
        ExportProgress(rec.store, job_id).report('normalizing', 90, 100)
        artifact = tmp_path / 'exports' / f'.{job_id}.progress'
        artifact.write_text('frame=90\n')
        async def leave_queued(self):
            pass
        with patch.object(Recorder, 'run_exports', leave_queued):
            await rec.start()
            try:
                row = rec.store.rows('SELECT status,progress FROM exports')[0]
                assert row == {'status': 'queued', 'progress': None}
                assert not artifact.exists()
            finally:
                await rec.stop()
    asyncio.run(scenario())


def test_encoder_start_failure_clears_progress_and_temporary_files(tmp_path):
    async def scenario():
        rec = Recorder(tmp_path, True)
        await rec.take_photo()
        with patch('app.service.subprocess.Popen', side_effect=FileNotFoundError('FFmpeg unavailable')):
            await rec.create_export()
            await rec.export_task
        job = rec.store.rows('SELECT * FROM exports')[0]
        assert job['status'] == 'failed' and job['progress'] is None
        assert 'FFmpeg unavailable' in job['error']
        assert not list((tmp_path / 'exports').iterdir())
    asyncio.run(scenario())
