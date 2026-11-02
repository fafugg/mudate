const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeApp(api, fetch = async () => ({})) {
  const context = vm.createContext({
    api, fetch, console, URL,
    setTimeout: fn => { queueMicrotask(fn); return 1; },
    clearInterval: () => {},
    localStorage: { getItem: () => null },
    VISIBLE_COLUMNS_DEFAULTS: {}, MAP_LEGEND: [],
  });
  const frontend = path.join(__dirname, '..');
  vm.runInContext(fs.readFileSync(path.join(frontend, 'map.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(frontend, 'app.js'), 'utf8'), context);
  const component = vm.runInContext('app()', context);
  component.username = 'ana';
  component.sessions = ['a', 'b', 'c'].map(id => ({ id, label: id }));
  component.showToast = (message, type) => { component.toast = { message, type }; };
  return component;
}

test('geocodes sessions sequentially, retries failures, and merges active session results', async () => {
  const calls = [];
  let cStarts = 0;
  const component = makeApp(async (method, url) => {
    calls.push(`${method} ${url}`);
    if (url.endsWith('/a/geocode?force=true')) return { run_id: 'r1' };
    if (url === '/runs/r1') return { status: 'done', progress: 1, total: 1, updated_houses: [{ internal_id: 'h', lat: 1, lng: 2, geocode_failed: false }] };
    if (url.endsWith('/b/geocode?force=true')) return { already_done: true };
    if (url.endsWith('/c/geocode?force=true')) return ++cStarts === 1 ? { already_running: true, run_id: 'other' } : { run_id: 'r2' };
    if (url === '/runs/other' || url === '/runs/r2') return { status: 'done' };
    throw new Error(`unexpected ${url}`);
  });
  component.screen = 'session';
  component.currentSession = { id: 'a' };
  component.houses = [{ internal_id: 'h', lat: null }];
  await component.geocodeAllSearches();
  assert.deepEqual(calls, [
    'POST /users/ana/sessions/a/geocode?force=true', 'GET /runs/r1',
    'POST /users/ana/sessions/b/geocode?force=true', 'POST /users/ana/sessions/c/geocode?force=true',
    'GET /runs/other', 'POST /users/ana/sessions/c/geocode?force=true', 'GET /runs/r2',
  ]);
  assert.equal(component.houses[0].lat, 1);
  assert.equal(component.bulkGeocode.completed, 3);
  assert.equal(component.bulkGeocode.running, false);
  assert.equal(component.toast.type, 'success');
  assert.match(component.toast.message, /2 búsquedas procesadas, 1 sin direcciones pendientes/);
});

test('stops rather than starting another run if polling fails', async () => {
  const calls = [];
  const component = makeApp(async (method, url) => {
    calls.push(url);
    if (method === 'POST') return { run_id: 'r1' };
    throw new Error('offline');
  });
  await component.geocodeAllSearches();
  assert.equal(calls.length, 2);
  assert.equal(component.bulkGeocode.running, false);
  assert.equal(component.toast.type, 'error');
  assert.match(component.toast.message, /detenida/);
});

test('a session start failure is reported and later sessions are attempted', async () => {
  const component = makeApp(async (method, url) => {
    if (url.includes('/a/')) throw new Error('deleted');
    return { already_done: true };
  });
  await component.geocodeAllSearches();
  assert.equal(component.bulkGeocode.completed, 3);
  assert.equal(component.toast.type, 'error');
  assert.match(component.toast.message, /2 sin direcciones pendientes.*1 con error/);
});

test('logging out during a start prevents later work or a stale toast', async () => {
  let resolveStart;
  const component = makeApp(() => new Promise(resolve => { resolveStart = resolve; }));
  const pending = component.geocodeAllSearches();
  component.logout();
  resolveStart({ run_id: 'r1' });
  await pending;
  assert.equal(component.bulkGeocode.running, false);
  assert.equal(component.toast, undefined);
});

test('leaving a shared geocoding run does not cancel it', () => {
  const requests = [];
  const component = makeApp(() => {}, (url, opts) => { requests.push([url, opts.method]); return Promise.resolve(); });
  component._geocodeRunId = 'shared';
  component._geocodeRunOwned = false;
  component.stopGeocoding();
  assert.equal(requests.length, 0);
  component._geocodeRunId = 'owned';
  component._geocodeRunOwned = true;
  component.stopGeocoding();
  assert.deepEqual(requests, [['/api/runs/owned', 'DELETE']]);
});

test('geocoding source attribution is escaped and only safe links are used', () => {
  const component = makeApp(() => {});
  const added = [], removed = [];
  const map = { attributionControl: { addAttribution: value => added.push(value), removeAttribution: value => removed.push(value) } };
  component._syncGeocodeAttribution(map, [{ geocode_attribution: '<source>', geocode_attribution_url: 'javascript:alert(1)' }], '_mapSourceAttributions');
  assert.deepEqual(added, ['&lt;source&gt;']);
  component._syncGeocodeAttribution(map, [{ geocode_attribution: 'Source', geocode_attribution_url: 'https://example.com' }], '_mapSourceAttributions');
  assert.deepEqual(removed, ['&lt;source&gt;']);
  assert.match(added[1], /href="https:\/\/example.com\/"/);
});
