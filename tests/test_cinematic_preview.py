"""Exercise preview request ordering and instant slider updates without a browser."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node required for UI logic checks')
def test_slider_recalculates_cached_results_and_ignores_outdated_requests():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(['node', '-e', r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const start = source.indexOf('let cinematicAnalysisKey =');
const end = source.indexOf('function updateExportEstimate()', start);
assert(start > 0 && end > start);
const nodes = new Map();
const node = id => {
  if (!nodes.has(id)) nodes.set(id, Object.assign(new EventTarget(), {value: '', checked: false, textContent: ''}));
  return nodes.get(id);
};
node('cinematic-focus').checked = true;
node('cinematic-reset-threshold').value = '45';
node('cinematic-zoom-percent').value = '40';
const timers = new Set(), requests = [];
const context = vm.createContext({
  $: node, AbortController, tab: 'videos', currentId: 'project-a',
  timeline: {id: 'project-a', cutoff: 100}, timelineLoading: false,
  rangePending: false, rangeInvalid: false, timelineRequest: 1,
  exportRange: {count: 156, start_frame_id: null, end_frame_id: null},
  endpoint: (id, path) => `${id}/${path}`,
  setTimeout: fn => {timers.add(fn); return fn;},
  clearTimeout: fn => timers.delete(fn),
  api: (path, method, body, signal) => new Promise(resolve => requests.push({path, body, signal, resolve})),
});
vm.runInContext(source.slice(start, end), context);
// Exercise the actual registered input/change listeners, not just their helper.
context.updateExportEstimate = () => {context.updateCinematicResetAnalysis(); return true;};
context.videoExportOptions = () => ({});
context.updateVideoButtons = () => context.updateCinematicResetAnalysis();
context.localStorage = {setItem: () => {}};
const listenersStart = source.indexOf('for (const id of ["smooth-motion"');
const listenersEnd = source.indexOf('let timeline = null', listenersStart);
assert(listenersStart > 0 && listenersEnd > listenersStart);
vm.runInContext(source.slice(listenersStart, listenersEnd), context);
const runTimers = () => {for (const fn of [...timers]) {timers.delete(fn); fn();}};
const flush = () => new Promise(setImmediate);
(async () => {
  context.updateCinematicResetAnalysis();
  runTimers();
  assert.equal(requests.length, 1);
  node('cinematic-reset-threshold').value = '70';
  node('cinematic-reset-threshold').dispatchEvent(new Event('input'));
  runTimers();
  assert.equal(requests.length, 1); // Moving the slider during analysis shares the work.
  requests[0].resolve({frames: 156, changes: [
    {photo: 10, score_percent: 8, orientation_change: false},
    {photo: 20, score_percent: 22, orientation_change: false},
    {photo: 30, score_percent: 0, orientation_change: false},
    {photo: 61, score_percent: 85, orientation_change: false},
    {photo: 136, score_percent: 64, orientation_change: false},
  ]});
  await flush();
  assert.match(node('cinematic-reset-count').textContent, /^1 zoom reset detected/);
  assert.equal(node('cinematic-reset-value').textContent, '70%');
  node('cinematic-reset-threshold').value = '45';
  node('cinematic-reset-threshold').dispatchEvent(new Event('change'));
  assert.match(node('cinematic-reset-count').textContent, /^2 zoom resets detected/);
  assert.match(node('cinematic-reset-count').textContent, /61, 136/);
  for (const [value, count] of [[10, 3], [0, 4], [100, 0], [45, 2]]) {
    node('cinematic-reset-threshold').value = String(value);
    node('cinematic-reset-threshold').dispatchEvent(new Event('input'));
    assert.match(node('cinematic-reset-count').textContent, new RegExp(`^${count} zoom resets detected`));
    assert.match(node('cinematic-reset-count').textContent, new RegExp(`at ${value}% threshold`));
  }
  assert.equal(requests.length, 1); // Recalculate immediately without more image reads.
  context.exportRange = {count: 2, start_frame_id: 'a', end_frame_id: 'b'};
  context.updateCinematicResetAnalysis();
  runTimers();
  context.exportRange = {count: 3, start_frame_id: 'a', end_frame_id: 'c'};
  context.updateCinematicResetAnalysis();
  runTimers();
  assert.equal(requests.length, 3);
  assert.equal(requests[1].signal.aborted, true);
  requests[1].resolve({frames: 2, changes: [{photo: 2, score_percent: 100, orientation_change: true}]});
  await flush();
  assert.match(node('cinematic-reset-count').textContent, /^Analyzing/);
  requests[2].resolve({frames: 3, changes: []});
  await flush();
  assert.match(node('cinematic-reset-count').textContent, /^0 zoom resets detected in 3/);
  node('cinematic-focus').checked = false;
  context.updateCinematicResetAnalysis();
  assert.equal(node('cinematic-reset-threshold').disabled, true);
  assert.match(node('cinematic-reset-count').textContent, /^Enable cinematic motion/);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''], cwd=root, check=True, capture_output=True, text=True)
