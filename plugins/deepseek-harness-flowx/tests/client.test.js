// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: exercise the browser client without external dependencies.
// Modified 2026-11-09: cover host restarts, plan labels, disabled nodes and build-risk consent.
// Modified 2026-11-09: exercise shared Fetch requests and transport failures.

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../client.js', import.meta.url), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const ok = value => ({ ok: true, value });
const fail = (code, message) => ({ ok: false, error: { code, message } });
const snapshot = (revision, status, extra = {}) => ({ instanceId: 'host-a', revision, status, workflowId: null, requestId: null, awaiting: null, result: null, error: null, ...extra });
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

// Hooks are only used by Page. Function children are expanded after Page renders.
// Timers are virtual so polling, cleanup and races are deterministic and fast.
function harness(rpc, fetchOverride) {
  const hooks = [], timers = new Map();
  let cursor = 0, tree, dirty = false, disposed = false, timerId = 0;
  const equalDeps = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const React = {
    Fragment: Symbol('Fragment'),
    createElement(type, props, ...children) { return { type, props: { ...(props || {}), children } }; },
    useState(initial) {
      const i = cursor++;
      if (!hooks[i]) {
        hooks[i] = { value: typeof initial === 'function' ? initial() : initial };
        hooks[i].set = next => {
          const value = typeof next === 'function' ? next(hooks[i].value) : next;
          if (!Object.is(value, hooks[i].value)) { hooks[i].value = value; dirty = true; }
        };
      }
      return [hooks[i].value, hooks[i].set];
    },
    useRef(initial) {
      const i = cursor++;
      if (!hooks[i]) hooks[i] = { current: initial };
      return hooks[i];
    },
    useCallback(fn, deps) {
      const i = cursor++;
      if (!hooks[i] || !equalDeps(hooks[i].deps, deps)) hooks[i] = { value: fn, deps };
      return hooks[i].value;
    },
    useEffect(effect, deps) {
      const i = cursor++;
      if (!hooks[i]) hooks[i] = {};
      if (!equalDeps(hooks[i].deps, deps)) {
        hooks[i].deps = deps;
        hooks[i].pending = effect;
      }
    },
  };
  let registration;
  const sandbox = {
    window: { __ModuleLoader__: { load(value) { assert.equal(registration, undefined); registration = value; } } },
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch: fetchOverride || (async (url, options) => {
      assert.equal(url, 'api/flowx');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['content-type'], 'application/json');
      assert.equal(options.credentials, 'same-origin');
      const { endpoint, payload } = JSON.parse(options.body);
      return new Response(JSON.stringify(await rpc(url, endpoint, payload)), { headers: { 'content-type': 'application/json' } });
    }),
  };
  vm.runInNewContext(source, sandbox, { filename: 'client.js' });
  const requires = [];
  const plugin = registration.factory(name => { requires.push(name); assert.equal(name, 'react'); return React; });
  const injections = [], registrations = [];
  const ctx = {
    slots: {
      inject(name, callback) { injections.push({ name, callback }); },
      register(options, component) { registrations.push({ options, component }); },
    },
  };
  plugin.apply(ctx);
  for (const injection of injections) injection.callback();
  const main = registrations.find(item => item.options.name === 'main');
  const Page = main.component;
  const props = main.options.inject();
  function render() {
    cursor = 0; dirty = false;
    tree = Page(props);
    for (const hook of hooks) {
      if (hook?.pending) {
        hook.cleanup?.();
        const effect = hook.pending;
        hook.pending = null;
        hook.cleanup = effect();
      }
    }
  }
  async function flush() {
    for (let i = 0; i < 12; i++) {
      await new Promise(resolve => setImmediate(resolve));
      if (dirty && !disposed) render();
    }
  }
  async function mount() { render(); await flush(); }
  async function tick() {
    const timer = [...timers.entries()].find(([, item]) => item.delay === 2000);
    assert.ok(timer, 'expected a two-second timer');
    timers.delete(timer[0]);
    timer[1].fn();
    await flush();
  }
  function unmount() {
    disposed = true;
    for (const hook of hooks) hook?.cleanup?.();
  }
  function all() {
    const result = [];
    function visit(node) {
      if (node == null || typeof node === 'boolean') return;
      if (Array.isArray(node)) { node.forEach(visit); return; }
      if (typeof node !== 'object') return;
      if (typeof node.type === 'function') { visit(node.type(node.props)); return; }
      result.push(node);
      visit(node.props.children);
    }
    visit(tree);
    return result;
  }
  function text(node) {
    if (node == null || typeof node === 'boolean') return '';
    if (Array.isArray(node)) return node.map(text).join('');
    if (typeof node !== 'object') return String(node);
    if (typeof node.type === 'function') return text(node.type(node.props));
    return text(node.props.children);
  }
  function find(predicate) { const found = all().find(predicate); assert.ok(found, 'element not found'); return found; }
  function button(label) { return find(node => node.type === 'button' && text(node) === label); }
  function input(id) { return find(node => node.props.id === id); }
  async function click(node) { assert.notEqual(node.props.disabled, true, 'control is disabled'); node.props.onClick({ preventDefault() {} }); await flush(); }
  async function change(id, value) { input(id).props.onChange({ target: { value } }); await flush(); }
  async function check(id, checked) { input(id).props.onChange({ target: { checked } }); await flush(); }
  async function submit(id) {
    const form = find(node => node.type === 'form' && text(node).includes(id));
    form.props.onSubmit({ preventDefault() {} });
    await flush();
  }
  return { registration, requires, plugin, injections, registrations, props, mount, flush, tick, unmount, all, text, find, button, input, click, change, check, submit, timers };
}

test('lazy loader registers exact slots and forwards Fetch requests with a stable call', async () => {
  const calls = [];
  const ui = harness((...args) => { calls.push(args); return ok('reply'); });
  assert.equal(ui.registration.id, 'dsh-flowx-plugin');
  assert.deepEqual(ui.requires, ['react']);
  assert.deepEqual(plain(ui.plugin.inject), ['slots']);
  assert.deepEqual(ui.injections.map(item => item.name), ['main', 'sidebar.panellist']);
  const [main, sidebar] = ui.registrations;
  assert.equal(main.options.name, 'main');
  assert.equal(main.options.key, 'flowx');
  assert.equal(typeof main.component, 'function');
  assert.deepEqual(plain(sidebar.options), { name: 'sidebar.panellist', id: 'flowx', order: 100, label: 'FlowX' });
  assert.equal(typeof sidebar.component, 'function');
  assert.equal(main.options.inject().call, ui.props.call);
  assert.equal(await ui.props.call('status', null), 'reply');
  assert.deepEqual(calls, [['api/flowx', 'status', null]]);
});

test('Fetch reports host, HTTP, malformed response, and network failures', async () => {
  let fetchResponse;
  const ui = harness(null, async () => {
    if (fetchResponse instanceof Error) throw fetchResponse;
    return fetchResponse;
  });
  fetchResponse = new Response(JSON.stringify(fail('flowx/stale-input', 'Repair changed.')), { status: 200 });
  await assert.rejects(ui.props.call('answer', {}), error => error.message === 'Repair changed.' && error.code === 'flowx/stale-input');
  fetchResponse = new Response(JSON.stringify(fail('flowx/invalid-request', 'Invalid JSON request.')), { status: 400 });
  await assert.rejects(ui.props.call('status', null), error => error.message === 'Invalid JSON request.' && error.code === 'flowx/invalid-request');
  fetchResponse = new Response('unauthorized', { status: 401 });
  await assert.rejects(ui.props.call('status', null), /HTTP 401/);
  fetchResponse = new Response(JSON.stringify(ok('not actually authorized')), { status: 403 });
  await assert.rejects(ui.props.call('status', null), /HTTP 403/);
  for (const body of ['not json', '{}', '{"ok":true}', '{"ok":false}']) {
    fetchResponse = new Response(body);
    await assert.rejects(ui.props.call('status', null), /response is invalid/i);
  }
  fetchResponse = new Error('private network details');
  await assert.rejects(ui.props.call('status', null), error => error.message === 'Unable to reach FlowX.');
});

test('HTTP admission errors are visible in the page', async () => {
  const ui = harness(null, async () => new Response('forbidden', { status: 403 }));
  await ui.mount();
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /HTTP 403/);
  ui.unmount();
});

test('polls a build, handles stale repair revisions, aborts, and reloads the catalog', async () => {
  const calls = [];
  let current = snapshot(1, 'idle');
  const ui = harness((channel, endpoint, payload) => {
    assert.equal(channel, 'api/flowx');
    calls.push({ endpoint, payload: plain(payload) });
    if (endpoint === 'status') return ok(current);
    if (endpoint === 'list') return ok({ workflows: { wf1: { nodes: {}, workflow_version: 1 } } });
    if (endpoint === 'start') return ok(current = snapshot(2, 'running', { workflowId: 'wf1' }));
    if (endpoint === 'answer') {
      current = snapshot(4, 'input_required', { workflowId: 'wf1', requestId: 'r2', awaiting: { stepId: 'repair2', prompt: 'Try again', test_log: null } });
      return fail('flowx/stale-input', 'The repair request is no longer current.');
    }
    if (endpoint === 'abort') return ok(current = snapshot(5, 'aborting', { workflowId: 'wf1' }));
    throw new Error(`unexpected ${endpoint}`);
  });
  await ui.mount();
  assert.deepEqual(calls.slice(0, 2), [{ endpoint: 'status', payload: null }, { endpoint: 'list', payload: null }]);
  assert.equal(ui.timers.size, 0);
  await ui.change('flowx-prompt', 'Build a useful workflow');
  await ui.check('flowx-build-consent', true);
  await ui.submit('Workflow prompt');
  assert.deepEqual(calls.at(-1), { endpoint: 'start', payload: { prompt: 'Build a useful workflow' } });
  assert.ok(ui.timers.size);
  current = snapshot(3, 'input_required', { workflowId: 'wf1', requestId: 'r1', awaiting: { stepId: 'repair1', prompt: 'Please fix', test_log: 'failure log' } });
  await ui.tick();
  assert.match(ui.text(ui.find(node => node.type === 'section' && ui.text(node).includes('Repair needs your input'))), /Please fix.*failure log/s);
  await ui.change('flowx-answer', 'old guidance');
  await ui.submit('Repair input');
  assert.deepEqual(calls.find(item => item.endpoint === 'answer').payload, { requestId: 'r1', stepId: 'repair1', revision: 3, input: 'old guidance' });
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /repair request changed/i);
  assert.equal(ui.input('flowx-answer').props.value, '');
  assert.match(ui.text(ui.find(node => node.type === 'section' && ui.text(node).includes('Repair needs your input'))), /Try again/);
  await ui.click(ui.button('Abort build'));
  assert.deepEqual(calls.find(item => item.endpoint === 'abort').payload, { requestId: 'r2', stepId: 'repair2', revision: 4 });
  current = snapshot(6, 'aborted', { workflowId: 'wf1' });
  await ui.tick();
  assert.equal(calls.filter(item => item.endpoint === 'list').length, 2);
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.all().some(node => node.props.id === 'flowx-answer'), false);
  ui.unmount();
});

test('inspects graph dependencies and reads requirement, main, and node artifacts', async () => {
  const calls = [];
  const graph = { nodes: [
    { name: 'Publish', desc: 'Send it', depends: ['Collect', 'Validate'], enable: false },
    { name: 'Collect', desc: 'Gather input', enabled: false },
    { name: 'Validate', desc: 'Check input', depends: ['Collect'], enable: true, enabled: false },
  ] };
  const artifacts = { workflow_id: 'wf1', workflow_version: 3, graph: 'plan/graph.json', requirement: 'plan/requirement.md', nodes: { Validate: { node_id: 'node-v', version: 2, artifacts: { source: 'nodes/validate.py', markdown: 'nodes/validate.md', log: null } } }, main_entrypoint: { path: 'main.py' } };
  const contents = { 'plan/graph.json': JSON.stringify(graph), 'plan/requirement.md': '# Requirements', 'main.py': 'print("main")', 'nodes/validate.md': '# Validate', 'nodes/validate.py': 'pass' };
  const ui = harness((channel, endpoint, payload) => {
    calls.push({ endpoint, payload: plain(payload) });
    if (endpoint === 'status') return ok(snapshot(1, 'idle'));
    if (endpoint === 'list') return ok({ workflows: { wf1: { workflow_version: 3, nodes: { Validate: 'node-v' } } } });
    if (endpoint === 'inspect') return ok({ artifacts });
    if (endpoint === 'readArtifact') return ok({ content: contents[payload.path], bytes: contents[payload.path].length });
    throw new Error(`unexpected ${endpoint}`);
  });
  await ui.mount();
  const workflow = ui.find(node => node.type === 'button' && ui.text(node).startsWith('wf1'));
  assert.match(ui.text(workflow), /Published workflow revision 3/);
  await ui.click(workflow);
  assert.deepEqual(calls.slice(-2), [{ endpoint: 'inspect', payload: { workflowId: 'wf1' } }, { endpoint: 'readArtifact', payload: { workflowId: 'wf1', path: 'plan/graph.json' } }]);
  const explorer = ui.find(node => node.type === 'section' && node.props['aria-labelledby'] === 'flowx-explorer-heading');
  assert.match(ui.text(explorer), /Published workflow revision 3.*Current plan dependency graph.*may differ after graph-only updates/s);
  const nodes = ui.all().filter(node => node.props['data-flowx-node']);
  assert.deepEqual(nodes.map(node => node.props['data-flowx-node']), ['Collect', 'Publish', 'Validate']);
  const edges = ui.all().filter(node => node.props['data-flowx-edge']).map(node => node.props['data-flowx-edge']);
  assert.deepEqual(edges, ['Collect → Publish', 'Validate → Publish', 'Collect → Validate']);
  const xs = Object.fromEntries(nodes.map(node => [node.props['data-flowx-node'], node.props.children.find(child => child.type === 'rect').props.x]));
  assert.ok(xs.Collect < xs.Validate && xs.Validate < xs.Publish);
  const disabled = ui.find(node => node.props['data-flowx-node'] === 'Publish');
  assert.match(disabled.props['aria-label'], /disabled/i);
  assert.match(ui.text(disabled), /Disabled/);
  assert.doesNotMatch(ui.text(ui.find(node => node.props['data-flowx-node'] === 'Collect')), /Disabled/);
  assert.doesNotMatch(ui.text(ui.find(node => node.props['data-flowx-node'] === 'Validate')), /Disabled/);
  disabled.props.onKeyDown({ key: ' ', preventDefault() {} });
  await ui.flush();
  assert.match(ui.text(ui.find(node => node.type === 'div' && ui.text(node).includes('Status in current plan: Disabled'))), /Send it.*Disabled.*Depends on: Collect, Validate/s);
  const validate = ui.find(node => node.props['data-flowx-node'] === 'Validate');
  assert.equal(validate.props.role, 'button');
  assert.equal(validate.props.tabIndex, 0);
  validate.props.onKeyDown({ key: 'Enter', preventDefault() {} });
  await ui.flush();
  assert.match(ui.text(ui.find(node => node.type === 'div' && ui.text(node).includes('Node ID: node-v'))), /Check input.*Enabled.*Depends on: Collect.*Published node revision 2/s);
  await ui.click(ui.button('markdown'));
  assert.equal(calls.at(-1).payload.path, 'nodes/validate.md');
  assert.equal(ui.text(ui.find(node => node.type === 'pre')), '# Validate');
  await ui.click(ui.button('Requirement'));
  assert.equal(calls.at(-1).payload.path, 'plan/requirement.md');
  assert.equal(ui.text(ui.find(node => node.type === 'pre')), '# Requirements');
  await ui.click(ui.button('Main entrypoint'));
  assert.equal(calls.at(-1).payload.path, 'main.py');
  assert.equal(ui.text(ui.find(node => node.type === 'pre')), 'print("main")');
  ui.unmount();
});

test('reports invalid graphs, missing dependencies and cycles without crashing', async () => {
  let graphContent = '{invalid';
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(snapshot(1, 'idle'));
    if (endpoint === 'list') return ok({ workflows: { wf1: { nodes: {} } } });
    if (endpoint === 'inspect') return ok({ artifacts: { graph: 'graph.json', nodes: {}, main_entrypoint: {} } });
    if (endpoint === 'readArtifact') return ok({ content: graphContent });
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.click(ui.find(node => node.type === 'button' && ui.text(node).startsWith('wf1')));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /Unable to load graph/);
  graphContent = JSON.stringify({ nodes: [{ name: 'a', depends: ['b', 'missing'] }, { name: 'b', depends: ['a'] }, { name: 'a' }, {}] });
  await ui.click(ui.find(node => node.type === 'button' && ui.text(node).startsWith('wf1')));
  const warning = ui.text(ui.find(node => node.props.role === 'status' && ui.text(node).includes('missing node')));
  assert.match(warning, /missing node missing/);
  assert.match(warning, /cycle/);
  assert.match(warning, /Duplicate node/);
  assert.equal(ui.all().filter(node => node.props['data-flowx-node']).length, 2);
  ui.unmount();
});

test('graph layout is stable across input ordering and artifact errors are visible', async () => {
  const graphNodes = [{ name: 'b', depends: ['a'] }, { name: 'c', depends: ['a'] }, { name: 'a' }, { name: 'd', depends: ['b', 'c'] }];
  let graph = graphNodes;
  const ui = harness((channel, endpoint, payload) => {
    if (endpoint === 'status') return ok(snapshot(1, 'idle'));
    if (endpoint === 'list') return ok({ workflows: { wf1: { nodes: {} } } });
    if (endpoint === 'inspect') return ok({ artifacts: { graph: 'graph.json', requirement: 'req.md', nodes: {}, main_entrypoint: {} } });
    if (endpoint === 'readArtifact' && payload.path === 'graph.json') return ok({ content: JSON.stringify({ nodes: graph }) });
    if (endpoint === 'readArtifact') return fail('flowx/artifact-unavailable', 'Artifact unavailable.');
    throw new Error(endpoint);
  });
  const coordinates = () => Object.fromEntries(ui.all().filter(node => node.props['data-flowx-node']).map(node => {
    const rect = node.props.children.find(child => child.type === 'rect');
    return [node.props['data-flowx-node'], [rect.props.x, rect.props.y]];
  }));
  await ui.mount();
  const workflow = () => ui.find(node => node.type === 'button' && ui.text(node).startsWith('wf1'));
  await ui.click(workflow());
  const first = coordinates();
  graph = [...graphNodes].reverse();
  await ui.click(workflow());
  assert.deepEqual(coordinates(), first);
  await ui.click(ui.button('Requirement'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /Artifact unavailable/);
  ui.unmount();
});

test('ignores a delayed older status and stops polling after unmount', async () => {
  const old = deferred();
  let statusCalls = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') {
      statusCalls++;
      if (statusCalls === 1) return ok(snapshot(1, 'idle'));
      if (statusCalls === 2) return ok(snapshot(3, 'input_required', { requestId: 'current', awaiting: { stepId: 'repair', prompt: 'Current', test_log: null } }));
      return old.promise;
    }
    if (endpoint === 'list') return ok({ workflows: {} });
    if (endpoint === 'start') return ok(snapshot(2, 'running'));
    if (endpoint === 'answer') return ok(snapshot(4, 'running'));
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.change('flowx-prompt', 'hello');
  await ui.check('flowx-build-consent', true);
  await ui.submit('Workflow prompt');
  await ui.tick();
  await ui.tick(); // Third status request is still in flight.
  assert.equal(statusCalls, 3);
  await ui.submit('Repair input');
  assert.equal(ui.all().some(node => node.props.id === 'flowx-answer'), false);
  old.resolve(ok(snapshot(3, 'input_required', { requestId: 'current', awaiting: { stepId: 'repair', prompt: 'Old', test_log: null } })));
  await ui.flush();
  assert.equal(ui.all().some(node => node.props.id === 'flowx-answer'), false);
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /running/i);
  ui.unmount();
  assert.equal(ui.timers.size, 0);
});

test('retries a transient busy catalog after build completion', async () => {
  let status = snapshot(1, 'running'), lists = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(status);
    if (endpoint === 'list') return ++lists === 1 ? fail('flowx/busy', 'Build cleanup is still running.') : ok({ workflows: { completed: { nodes: {} } } });
    throw new Error(endpoint);
  });
  await ui.mount();
  assert.equal(lists, 0);
  status = snapshot(2, 'completed', { workflowId: 'completed', result: { ok: true, status: 'completed', workflowId: 'completed' } });
  await ui.tick();
  assert.equal(lists, 1);
  await ui.tick();
  assert.equal(lists, 2);
  assert.ok(ui.find(node => node.type === 'button' && ui.text(node).startsWith('completed')));
  ui.unmount();
});

test('refreshes the catalog when a build finishes while inspection is pending', async () => {
  const inspection = deferred();
  let status = snapshot(1, 'running'), lists = 0, graphReads = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(status);
    if (endpoint === 'list') { lists++; return ok({ workflows: { wf1: { nodes: {} } } }); }
    if (endpoint === 'inspect') return inspection.promise;
    if (endpoint === 'readArtifact') { graphReads++; return ok({ content: '{"nodes":[]}' }); }
    throw new Error(endpoint);
  });
  await ui.mount();
  status = snapshot(2, 'completed');
  await ui.tick();
  assert.equal(lists, 1);
  await ui.click(ui.find(node => node.type === 'button' && ui.text(node).startsWith('wf1')));
  status = snapshot(3, 'running');
  await ui.click(ui.button('Refresh status'));
  status = snapshot(4, 'completed');
  await ui.tick();
  assert.equal(lists, 1);
  inspection.resolve(ok({ artifacts: { graph: 'graph.json', nodes: {}, main_entrypoint: {} } }));
  await ui.flush();
  assert.equal(graphReads, 0);
  assert.equal(lists, 2);
  assert.equal(ui.all().some(node => node.props.id === 'flowx-explorer-heading'), false);
  ui.unmount();
});

test('reconciles status after a failed start so another client’s active build is visible', async () => {
  let status = snapshot(1, 'idle');
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(status);
    if (endpoint === 'list') return ok({ workflows: {} });
    if (endpoint === 'start') {
      status = snapshot(2, 'running', { workflowId: 'external' });
      return fail('flowx/busy', 'A build is active.');
    }
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.change('flowx-prompt', 'test');
  await ui.check('flowx-build-consent', true);
  await ui.submit('Workflow prompt');
  assert.equal(ui.input('flowx-prompt').props.disabled, true);
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /running/i);
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /build is active/i);
  ui.unmount();
});

test('requires accessible, explicit consent for every build attempt', async () => {
  let starts = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(snapshot(1, 'idle'));
    if (endpoint === 'list') return ok({ workflows: {} });
    if (endpoint === 'start') {
      starts++;
      return starts === 1 ? fail('flowx/start-failed', 'Start failed.') : ok(snapshot(2, 'running'));
    }
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.change('flowx-prompt', 'build something');
  const consent = ui.input('flowx-build-consent');
  assert.equal(consent.type, 'input');
  assert.equal(consent.props.type, 'checkbox');
  assert.equal(consent.props.checked, false);
  assert.equal(consent.props.required, true);
  assert.equal(consent.props['aria-describedby'], 'flowx-build-risk');
  assert.match(ui.text(ui.find(node => node.type === 'label' && node.props.htmlFor === 'flowx-build-consent')), /understand and accept/i);
  const risk = ui.text(ui.input('flowx-build-risk'));
  assert.match(risk, /generated code.*pytest.*without a sandbox.*host privileges/i);
  assert.match(risk, /API credentials.*local files.*network access/i);
  assert.match(risk, /sensitive data/i);
  assert.equal(ui.button('Build workflow').props.disabled, true);
  await ui.submit('Workflow prompt'); // Even a programmatic submit must not bypass consent.
  assert.equal(starts, 0);
  await ui.check('flowx-build-consent', true);
  assert.equal(ui.button('Build workflow').props.disabled, false);
  await ui.submit('Workflow prompt');
  assert.equal(starts, 1);
  assert.equal(ui.input('flowx-build-consent').props.checked, false);
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /start failed/i);
  await ui.submit('Workflow prompt');
  assert.equal(starts, 1);
  await ui.check('flowx-build-consent', true);
  await ui.submit('Workflow prompt');
  assert.equal(starts, 2);
  assert.equal(ui.input('flowx-build-consent').props.checked, false);
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /running/i);
  ui.unmount();
});

test('an idle host restart clears the explorer and replaces an in-flight catalog', async () => {
  const oldList = deferred();
  let status = snapshot(100, 'idle'), lists = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(status);
    if (endpoint === 'list') {
      lists++;
      if (lists === 1) return ok({ workflows: { old: { nodes: {} } } });
      if (lists === 2) return oldList.promise;
      return ok({ workflows: { fresh: { nodes: {} } } });
    }
    if (endpoint === 'inspect') return ok({ artifacts: { graph: 'graph.json', nodes: {}, main_entrypoint: {} } });
    if (endpoint === 'readArtifact') return ok({ content: JSON.stringify({ nodes: [{ name: 'OldNode' }] }) });
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.click(ui.find(node => node.type === 'button' && ui.text(node).startsWith('old')));
  assert.ok(ui.find(node => node.props['data-flowx-node'] === 'OldNode'));
  await ui.click(ui.button('Refresh'));
  assert.equal(lists, 2);
  status = snapshot(0, 'idle', { instanceId: 'host-b' });
  await ui.click(ui.button('Refresh status'));
  assert.equal(ui.all().some(node => node.props.id === 'flowx-explorer-heading'), false);
  assert.equal(ui.all().some(node => node.props['data-flowx-node'] === 'OldNode'), false);
  assert.equal(lists, 2); // Refresh waits for the old inspection operation to settle.
  oldList.resolve(ok({ workflows: { stale: { nodes: {} } } }));
  await ui.flush();
  assert.equal(lists, 3);
  assert.ok(ui.find(node => node.type === 'button' && ui.text(node).startsWith('fresh')));
  assert.equal(ui.all().some(node => node.type === 'button' && ui.text(node).startsWith('stale')), false);
  ui.unmount();
});

test('a restarted host rejects late responses from old instances and older requests', async () => {
  const old = deferred(), unknown = deferred();
  let statusCalls = 0, lists = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') {
      statusCalls++;
      if (statusCalls === 1) return ok(snapshot(100, 'running'));
      if (statusCalls === 2) return old.promise;
      if (statusCalls === 3) return unknown.promise;
      if (statusCalls === 4) return ok(snapshot(0, 'idle', { instanceId: 'host-b' }));
      return ok(snapshot(1, 'completed', { instanceId: 'host-b' }));
    }
    if (endpoint === 'list') { lists++; return ok({ workflows: { fresh: { nodes: {} } } }); }
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.tick(); // A request from host-a remains in flight.
  await ui.click(ui.button('Refresh status')); // Another pre-restart request.
  await ui.click(ui.button('Refresh status')); // host-b starts at revision zero.
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /idle/i);
  assert.equal(lists, 1);
  assert.equal(ui.timers.size, 0);
  old.resolve(ok(snapshot(999, 'input_required', { requestId: 'old', awaiting: { stepId: 'old', prompt: 'Old host' } })));
  unknown.resolve(ok(snapshot(9999, 'running', { instanceId: 'host-c' })));
  await ui.flush();
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /idle/i);
  assert.equal(ui.all().some(node => node.props.id === 'flowx-answer'), false);
  assert.equal(lists, 1);
  await ui.click(ui.button('Refresh status'));
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /completed/i);
  ui.unmount();
});

test('overlapping initial requests can discover a host restarted before the second response', async () => {
  const first = deferred(), second = deferred();
  let statusCalls = 0, lists = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ++statusCalls === 1 ? first.promise : second.promise;
    if (endpoint === 'list') { lists++; return ok({ workflows: {} }); }
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.click(ui.button('Refresh status'));
  first.resolve(ok(snapshot(30, 'idle')));
  await ui.flush();
  assert.equal(lists, 1);
  second.resolve(ok(snapshot(0, 'idle', { instanceId: 'host-b' })));
  await ui.flush();
  assert.equal(lists, 2);
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /idle/i);
  ui.unmount();
});

test('a late inspection from a previous host cannot restore its graph after restart', async () => {
  const inspection = deferred();
  let status = snapshot(80, 'idle'), lists = 0, graphReads = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(status);
    if (endpoint === 'list') {
      lists++;
      return ok({ workflows: { [lists === 1 ? 'old' : 'fresh']: { nodes: {} } } });
    }
    if (endpoint === 'inspect') return inspection.promise;
    if (endpoint === 'readArtifact') { graphReads++; return ok({ content: '{"nodes":[]}' }); }
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.click(ui.find(node => node.type === 'button' && ui.text(node).startsWith('old')));
  status = snapshot(0, 'idle', { instanceId: 'host-b' });
  await ui.click(ui.button('Refresh status'));
  inspection.resolve(ok({ artifacts: { graph: 'graph.json', nodes: {}, main_entrypoint: {} } }));
  await ui.flush();
  assert.equal(graphReads, 0);
  assert.equal(lists, 2);
  assert.ok(ui.find(node => node.type === 'button' && ui.text(node).startsWith('fresh')));
  assert.equal(ui.all().some(node => node.props.id === 'flowx-explorer-heading'), false);
  ui.unmount();
});

test('a pending start response cannot restore an old host after restart', async () => {
  const pending = deferred();
  let status = snapshot(42, 'idle'), lists = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(status);
    if (endpoint === 'list') { lists++; return ok({ workflows: {} }); }
    if (endpoint === 'start') return pending.promise;
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.change('flowx-prompt', 'build');
  await ui.check('flowx-build-consent', true);
  await ui.submit('Workflow prompt');
  status = snapshot(0, 'idle', { instanceId: 'host-b' });
  await ui.click(ui.button('Refresh status'));
  assert.equal(lists, 1);
  pending.resolve(ok(snapshot(900, 'running')));
  await ui.flush();
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /idle/i);
  assert.equal(lists, 2);
  assert.equal(ui.timers.size, 0);
  ui.unmount();
});

test('a same-host status refresh does not hide a pending start failure', async () => {
  const pending = deferred();
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(snapshot(1, 'idle'));
    if (endpoint === 'list') return ok({ workflows: {} });
    if (endpoint === 'start') return pending.promise;
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.change('flowx-prompt', 'build');
  await ui.check('flowx-build-consent', true);
  await ui.submit('Workflow prompt');
  await ui.click(ui.button('Refresh status'));
  pending.resolve(fail('flowx/start-failed', 'Start failed.'));
  await ui.flush();
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /start failed/i);
  ui.unmount();
});

test('late state failures, retired instances and missing IDs cannot replace the current host', async () => {
  const oldError = deferred();
  let status = snapshot(10, 'idle'), delayed = false;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') {
      if (delayed) { delayed = false; return oldError.promise; }
      return ok(status);
    }
    if (endpoint === 'list') return ok({ workflows: {} });
    throw new Error(endpoint);
  });
  await ui.mount();
  delayed = true;
  await ui.click(ui.button('Refresh status'));
  status = snapshot(0, 'idle', { instanceId: 'host-b' });
  await ui.click(ui.button('Refresh status'));
  oldError.resolve(fail('flowx/disposed', 'Old host disposed.'));
  await ui.flush();
  assert.equal(ui.all().some(node => node.props.role === 'alert'), false);
  status = snapshot(999, 'running'); // Previously observed IDs remain retired.
  await ui.click(ui.button('Refresh status'));
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /idle/i);
  status = snapshot(1000, 'running', { instanceId: undefined });
  await ui.click(ui.button('Refresh status'));
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /idle/i);
  assert.equal(ui.all().some(node => /restart detection is unavailable/i.test(ui.text(node))), false);
  ui.unmount();
});

test('same-instance revisions remain authoritative when status responses arrive out of order', async () => {
  const olderRequest = deferred(), laterRequest = deferred();
  let statusCalls = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') {
      statusCalls++;
      if (statusCalls === 1) return ok(snapshot(1, 'idle'));
      return statusCalls === 2 ? olderRequest.promise : laterRequest.promise;
    }
    if (endpoint === 'list') return ok({ workflows: {} });
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.click(ui.button('Refresh status'));
  await ui.click(ui.button('Refresh status'));
  laterRequest.resolve(ok(snapshot(3, 'running')));
  await ui.flush();
  olderRequest.resolve(ok(snapshot(4, 'completed')));
  await ui.flush();
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /completed/i);
  ui.unmount();
});

test('legacy hosts remain usable but disclose that restart detection is unavailable', async () => {
  let status = snapshot(1, 'idle', { instanceId: undefined }), lists = 0;
  const ui = harness((channel, endpoint) => {
    if (endpoint === 'status') return ok(status);
    if (endpoint === 'list') { lists++; return ok({ workflows: {} }); }
    throw new Error(endpoint);
  });
  await ui.mount();
  assert.ok(ui.find(node => node.props.role === 'status' && /restart detection is unavailable/i.test(ui.text(node))));
  status = snapshot(0, 'idle', { instanceId: 'host-b' });
  await ui.click(ui.button('Refresh status'));
  assert.equal(lists, 2);
  assert.equal(ui.all().some(node => /restart detection is unavailable/i.test(ui.text(node))), false);
  ui.unmount();
});