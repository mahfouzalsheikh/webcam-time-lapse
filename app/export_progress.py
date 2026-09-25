"""Persist stage progress and read FFmpeg's machine-readable progress stream."""

import json
import time


class ExportProgress:
    def __init__(self, store, job_id):
        self.store, self.job_id = store, job_id
        self.started_at = time.time()
        self.state = None
        self.last_write = float('-inf')

    def report(self, stage, completed, total):
        now, tick = time.time(), time.monotonic()
        completed = max(0, min(completed, total))
        changed_stage = self.state is None or self.state['stage'] != stage
        previous = self.state
        self.state = {
            'stage': stage, 'completed': completed, 'total': total,
            'started_at': self.started_at,
            'stage_started_at': now if changed_stage else previous['stage_started_at'],
            'last_advanced_at': now if changed_stage or completed != previous['completed'] else previous['last_advanced_at'],
        }
        # Persist stage changes/endpoints immediately, and at most once a second
        # between them. Browser polling never scans thousands of image files.
        if changed_stage or completed == total or tick - self.last_write >= 1:
            with self.store.connect() as db:
                db.execute("UPDATE exports SET progress=? WHERE id=? AND status='running'",
                           (json.dumps(self.state), self.job_id))
            self.last_write = tick


def progress_details(job, now=None):
    if job.get('status') != 'running' or not job.get('progress'):
        return None
    state = json.loads(job['progress'])
    now = time.time() if now is None else now
    elapsed = max(0, now - state['stage_started_at'])
    completed, total = state['completed'], state['total']
    since_advance = max(0, now - state['last_advanced_at'])
    # An ETA for an unstarted stage would be a guess. Suppress stale estimates
    # if counts have not advanced for a minute, rather than promise a finish time.
    rate = completed / elapsed if completed >= 3 and elapsed >= 3 and since_advance < 60 else None
    stages = ['analyzing', 'normalizing', 'encoding', 'finalizing'] if job.get('normalize_lighting') else ['encoding', 'finalizing']
    if job.get('timing_overlay'):
        stages.insert(stages.index('encoding'), 'overlay')
    if job.get('cinematic_focus'):
        position = stages.index('overlay') if 'overlay' in stages else stages.index('encoding')
        stages[position:position] = ['focus_analysis', 'focusing']
    return {
        **state,
        'stage_number': stages.index(state['stage']) + 1,
        'stage_count': len(stages),
        'percent': round(completed / total * 100, 1) if total else None,
        'elapsed_seconds': max(0, now - state['started_at']),
        'stage_elapsed_seconds': elapsed,
        'seconds_since_progress': since_advance,
        'units_per_second': rate,
        'eta_seconds': (total - completed) / rate if rate and completed < total else None,
    }


class FFmpegProgress:
    def __init__(self, report, total):
        self.report, self.total = report, total
        self.pending = ''
        self.frames = 0

    def read(self, handle):
        self.feed(handle.read(65536))

    def feed(self, text):
        lines = (self.pending + text).split('\n')
        self.pending = lines.pop()
        for line in lines:
            key, _, value = line.strip().partition('=')
            if key == 'frame':
                try:
                    self.frames = max(self.frames, min(self.total, int(value)))
                except ValueError:
                    pass
            elif key == 'progress':
                if value == 'end' or self.frames == self.total:
                    self.report('finalizing', 0, 0)
                elif value == 'continue':
                    self.report('encoding', self.frames, self.total)
