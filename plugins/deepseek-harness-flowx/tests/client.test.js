// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: exercise the browser client without external dependencies.
// Modified 2026-11-09: cover host restarts, plan labels, disabled nodes and build-risk consent.
// Modified 2026-11-09: exercise shared Fetch requests and transport failures.
// Modified 2026-11-10: test the read-only footer overlay, settings, folder browser and asynchronous races.
// Modified 2026-11-10: verify navigation drawer handoff in narrow windows.

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../client.js', import.meta.url), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const ok = value => ({ ok: true, value });
const fail = (code, message) => ({ ok: false, error: { code, message } });
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
const settings = { python: '/bin/python', workspace: '/workspace', apiKey: '' };
const catalog = (...ids) => ({ workflows: Object.fromEntries(ids.map(id => [id, { workflow_version: 3, nodes: { n: 'id' } }])) });
const entries = (...items) => ({ entries: items, truncated: false });
const entry = (path, type = 'file', readable = true) => ({ name: path.split('/').at(-1), path, type, readable });
const artifactMap = extra => ({ workflow_version: 3, nodes: {}, main_entrypoint: {}, ...extra });

// Each mounted hook component has its own hook state and effect cleanup. Overlay
// supplies the actual Page component and transport; Graph and Folder are pure.
function harness(rpc, fetchOverride) {
  let active, registration, launcher, overlay, page, pageElement;
  const focus = [], requires = [], injections = [], registrations = [], selections = [], drawerToggles = [];
  let drawer = false;
  const equalDeps = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const React = {
    Fragment: Symbol('Fragment'),
    createElement(type, props, ...children) { return { type, props: { ...(props || {}), children } }; },
    useState(initial) {
      const owner = active, i = owner.cursor++;
      if (!owner.hooks[i]) {
        const hook = { value: typeof initial === 'function' ? initial() : initial };
        hook.set = next => {
          if (owner.disposed) return;
          const value = typeof next === 'function' ? next(hook.value) : next;
          if (!Object.is(value, hook.value)) { hook.value = value; owner.dirty = true; }
        };
        owner.hooks[i] = hook;
      }
      const hook = owner.hooks[i];
      return [hook.value, hook.set];
    },
    useRef(initial) {
      const owner = active, i = owner.cursor++;
      return owner.hooks[i] ||= { current: initial };
    },
    useCallback(fn, deps) {
      const owner = active, i = owner.cursor++;
      if (!owner.hooks[i] || !equalDeps(owner.hooks[i].deps, deps)) owner.hooks[i] = { value: fn, deps };
      return owner.hooks[i].value;
    },
    useEffect(effect, deps) {
      const owner = active, i = owner.cursor++;
      const hook = owner.hooks[i] ||= {};
      if (!equalDeps(hook.deps, deps)) { hook.deps = deps; hook.pending = effect; }
    },
  };
  const sandbox = {
    window: { __ModuleLoader__: { load(value) { assert.equal(registration, undefined); registration = value; } } },
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
  const plugin = registration.factory(name => { requires.push(name); assert.equal(name, 'react'); return React; });
  plugin.apply({ layout: { toggleSidebar() { drawerToggles.push(true); drawer = false; } }, slots: {
    inject(name, callback) { injections.push({ name, callback }); },
    register(options, component) { registrations.push({ options, component }); },
    select(...args) { selections.push(args); },
  } });
  for (const injection of injections) injection.callback();
  const Launcher = registrations.find(item => item.options.name === 'sidebar.footer.action')?.component;
  const Overlay = registrations.find(item => item.options.name === 'shell.overlay')?.component;

  function runner(component, props) {
    return { component, props, hooks: [], cursor: 0, tree: null, dirty: true, disposed: false, refs: new Set() };
  }
  function text(node) {
    if (node == null || typeof node === 'boolean') return '';
    if (Array.isArray(node)) return node.map(text).join('');
    if (typeof node !== 'object') return String(node);
    return text(node.props.children);
  }
  function expand(node, refs) {
    if (Array.isArray(node)) return node.map(child => expand(child, refs));
    if (node == null || typeof node !== 'object') return node;
    if (typeof node.type === 'function') return expand(node.type(node.props), refs);
    const children = expand(node.props.children, refs);
    if (node.props.ref) {
      const ref = node.props.ref;
      refs.add(ref);
      if (!ref.current) ref.current = { focus() { focus.push(node.props.id || node.props['aria-label'] || text(children)); } };
    }
    return { ...node, props: { ...node.props, children } };
  }
  function render(owner, expandChildren = true) {
    owner.cursor = 0; owner.dirty = false; active = owner;
    const raw = owner.component(owner.props);
    active = null;
    const refs = new Set();
    owner.tree = expandChildren ? expand(raw, refs) : raw;
    for (const ref of owner.refs) if (!refs.has(ref)) ref.current = null;
    owner.refs = refs;
    for (const hook of owner.hooks) {
      if (hook?.pending) {
        hook.cleanup?.();
        const effect = hook.pending;
        hook.pending = null;
        hook.cleanup = effect();
      }
    }
  }
  function dispose(owner) {
    if (!owner || owner.disposed) return;
    owner.disposed = true;
    for (const hook of owner.hooks) hook?.cleanup?.();
    for (const ref of owner.refs) ref.current = null;
  }
  function update() {
    if (launcher?.dirty) render(launcher);
    if (overlay?.dirty) {
      render(overlay, false);
      pageElement = overlay.tree;
      if (!pageElement) { dispose(page); page = null; }
      else if (!page) page = runner(pageElement.type, pageElement.props);
      else page.props = pageElement.props;
    }
    if (page?.dirty) render(page);
  }
  async function flush() {
    for (let i = 0; i < 12; i++) {
      update();
      await new Promise(resolve => setImmediate(resolve));
    }
    update();
  }
  async function mount(wide = true) {
    assert.equal(typeof Launcher, 'function');
    assert.equal(typeof Overlay, 'function');
    launcher = runner(Launcher, { wide });
    overlay = runner(Overlay, {});
    await flush();
  }
  function all() {
    const result = [];
    function visit(node) {
      if (node == null || typeof node === 'boolean') return;
      if (Array.isArray(node)) { node.forEach(visit); return; }
      if (typeof node !== 'object') return;
      result.push(node);
      visit(node.props.children);
    }
    if (page) visit(page.tree);
    return result;
  }
  function find(predicate) { const found = all().find(predicate); assert.ok(found, 'element not found'); return found; }
  function button(label) { return find(node => node.type === 'button' && text(node) === label); }
  function workflow(id) { return find(node => node.type === 'button' && text(node).startsWith(`${id}Published revision `)); }
  function path(name) { return find(node => node.props['data-flowx-path'] === name); }
  function input(id) { return find(node => node.type === 'input' && node.props.id === `flowx-${id}`); }
  function fire(node) {
    assert.notEqual(node.props.disabled, true, 'control is disabled');
    assert.equal(typeof node.props.onClick, 'function');
    node.props.onClick({ currentTarget: trigger, preventDefault() {} });
  }
  const trigger = { focus() { focus.push('launcher'); }, closest() { return drawer ? {} : null; }, ownerDocument: { querySelector() { return { focus() { focus.push('sidebar opener'); } }; } } };
  async function click(node) { fire(node); await flush(); }
  async function toggle() { await click(launcher.tree); }
  async function change(id, value) { input(id).props.onChange({ target: { value } }); await flush(); }
  async function submit() {
    let prevented = false;
    find(node => node.type === 'form').props.onSubmit({ preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
    await flush();
  }
  async function key(node, key) {
    const event = { key, prevented: false, stopped: false, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
    node.props.onKeyDown(event);
    await flush();
    return event;
  }
  function unmount() { dispose(page); dispose(overlay); dispose(launcher); page = null; }
  return {
    registration, requires, plugin, injections, registrations, selections, focus, drawerToggles,
    setDrawer(value) { drawer = value; },
    mount, flush, unmount, all, find, text, button, workflow, path, input, fire, click, toggle, change, submit, key,
    launcher: () => launcher.tree, overlay: () => overlay.tree, page: () => page?.tree,
    call: (...args) => pageElement.props.call(...args),
  };
}

test('client script has valid JavaScript syntax', () => {
  assert.doesNotThrow(() => new vm.Script(source, { filename: 'client.js' }));
});

test('registers the footer launcher and shell overlay without selecting a main slot', async () => {
  const calls = [];
  const ui = harness((url, endpoint, payload) => { calls.push({ url, endpoint, payload }); return ok(catalog()); });
  assert.equal(ui.registration.id, 'dsh-flowx-plugin');
  assert.deepEqual(ui.requires, ['react']);
  assert.deepEqual(plain(ui.plugin.inject), ['slots', 'layout']);
  assert.deepEqual(ui.injections.map(item => item.name), ['sidebar.footer.action', 'shell.overlay']);
  assert.deepEqual(ui.registrations.map(item => plain(item.options)), [
    { name: 'sidebar.footer.action', id: 'flowx', order: 100 },
    { name: 'shell.overlay', id: 'flowx', order: 100 },
  ]);
  await ui.mount();
  assert.equal(ui.overlay(), null);
  assert.equal(ui.launcher().props['aria-expanded'], false);
  assert.equal(ui.launcher().props['aria-controls'], 'flowx-overlay');
  assert.match(ui.text(ui.launcher()), /FlowX/);
  assert.deepEqual(calls, []);
  await ui.toggle();
  assert.equal(ui.launcher().props['aria-expanded'], true);
  assert.equal(ui.page().props.role, 'region');
  assert.equal(ui.page().props['aria-label'], 'FlowX workflow explorer');
  assert.deepEqual(calls, [{ url: 'api/flowx', endpoint: 'list', payload: null }]);
  assert.equal(ui.focus[0], 'Close FlowX');
  await ui.click(ui.button('Close'));
  assert.equal(ui.overlay(), null);
  assert.equal(ui.launcher().props['aria-expanded'], false);
  assert.equal(ui.focus.at(-1), 'launcher');
  await ui.toggle();
  await ui.toggle();
  assert.equal(ui.overlay(), null);
  assert.equal(calls.length, 2);
  assert.deepEqual(ui.selections, []);
  ui.unmount();
});

test('collapsed launcher exposes its accessible name without a wide label', async () => {
  const ui = harness(() => ok(catalog()));
  await ui.mount(false);
  assert.equal(ui.launcher().props['aria-label'], 'FlowX workflows');
  assert.equal(ui.text(ui.launcher()), '');
  await ui.toggle();
  assert.equal(ui.launcher().props['aria-expanded'], true);
  ui.unmount();
});

test('opening from a modal navigation drawer dismisses it without changing the conversation', async () => {
  const ui = harness(() => ok(catalog()));
  await ui.mount();
  ui.setDrawer(true);
  await ui.toggle();
  assert.equal(ui.drawerToggles.length, 1);
  assert.ok(ui.page());
  assert.deepEqual(ui.selections, []);
  await ui.click(ui.button('Close'));
  assert.equal(ui.focus.at(-1), 'sidebar opener');
  await ui.toggle();
  assert.equal(ui.drawerToggles.length, 1);
  ui.unmount();
});

test('transport forwards Fetch requests and reports host, HTTP, malformed and network errors', async () => {
  let response = new Response(JSON.stringify(ok(catalog()))), request;
  const ui = harness(null, async (url, options) => {
    request = { url, options };
    if (response instanceof Error) throw response;
    return response.clone();
  });
  await ui.mount();
  await ui.toggle();
  response = new Response(JSON.stringify(ok('reply')));
  assert.equal(await ui.call('inspect', { workflowId: 'wf1' }), 'reply');
  assert.equal(request.url, 'api/flowx');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.headers['content-type'], 'application/json');
  assert.equal(request.options.credentials, 'same-origin');
  assert.deepEqual(JSON.parse(request.options.body), { endpoint: 'inspect', payload: { workflowId: 'wf1' } });
  response = new Response(JSON.stringify(fail('flowx/busy', 'Try again.')), { status: 400 });
  await assert.rejects(ui.call('list', null), error => error.message === 'Try again.' && error.code === 'flowx/busy');
  for (const body of ['unauthorized', JSON.stringify(ok('not authorized'))]) {
    response = new Response(body, { status: 403 });
    await assert.rejects(ui.call('list', null), /HTTP 403/);
  }
  for (const body of ['not json', '{}', '{"ok":true}', '{"ok":false}']) {
    response = new Response(body);
    await assert.rejects(ui.call('list', null), /response is invalid/i);
  }
  response = new Error('private network details');
  await assert.rejects(ui.call('list', null), error => error.message === 'Unable to reach FlowX.');
  ui.unmount();
});

test('catalog loading, ordering, empty state, refresh and visible errors', async () => {
  const pending = deferred();
  let lists = 0;
  const ui = harness((url, endpoint) => {
    assert.equal(endpoint, 'list');
    lists++;
    if (lists === 1) return pending.promise;
    if (lists === 2) return ok(catalog('zeta', 'alpha'));
    if (lists === 3) return ok({ workflows: [] });
    return fail('flowx/configuration', 'Workspace unavailable.');
  });
  await ui.mount();
  await ui.toggle();
  assert.match(ui.text(ui.find(node => node.props.role === 'status')), /Loading workflows/);
  assert.equal(ui.button('Refresh').props.disabled, true);
  pending.resolve(ok(catalog()));
  await ui.flush();
  assert.match(ui.text(ui.page()), /No workflows found/);
  await ui.click(ui.button('Refresh'));
  assert.deepEqual(ui.all().filter(node => node.type === 'button' && /Published revision/.test(ui.text(node))).map(node => ui.text(node).split('Published')[0]), ['alpha', 'zeta']);
  assert.match(ui.text(ui.workflow('alpha')), /Published revision 3 · 1 listed nodes/);
  await ui.click(ui.button('Refresh'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /catalog is invalid/i);
  await ui.click(ui.button('Refresh'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /Workspace unavailable/);
  ui.unmount();
});

test('HTTP admission failures appear in the overlay', async () => {
  const ui = harness(null, async () => new Response('forbidden', { status: 403 }));
  await ui.mount();
  await ui.toggle();
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /HTTP 403/);
  ui.unmount();
});

test('inspects graph dependencies, descriptions, node state and selectable artifacts', async () => {
  const calls = [];
  const graph = { nodes: [
    { name: 'Publish', desc: 'Send it', depends: ['Validate', 'Collect', 'Collect'], enable: false },
    { name: 'Collect', desc: 'Gather input' },
    { name: 'Validate', desc: 'Check input', depends: ['Collect'] },
  ] };
  const artifacts = artifactMap({ graph: 'plan/graph.json', requirement: 'plan/requirement.md', main_entrypoint: { path: 'main.py' }, nodes: { Validate: { node_id: 'node-v', version: 2, artifacts: { source: 'nodes/validate.py', markdown: 'nodes/validate.md', log: null } } } });
  const contents = { 'plan/graph.json': JSON.stringify(graph), 'plan/requirement.md': '# Requirements', 'main.py': 'print("main")', 'nodes/validate.md': '# Validate', 'nodes/validate.py': 'pass' };
  const ui = harness((url, endpoint, payload) => {
    calls.push({ endpoint, payload });
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return ok({ artifacts });
    if (endpoint === 'readArtifact') return ok({ content: contents[payload.path], bytes: contents[payload.path].length });
    if (endpoint === 'listFolder') return ok(entries());
    throw new Error(`unexpected ${endpoint}`);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('wf1'));
  assert.deepEqual(calls.slice(1), [
    { endpoint: 'inspect', payload: { workflowId: 'wf1' } },
    { endpoint: 'readArtifact', payload: { workflowId: 'wf1', path: 'plan/graph.json' } },
    { endpoint: 'listFolder', payload: { workflowId: 'wf1', path: '' } },
  ]);
  assert.equal(ui.workflow('wf1').props['aria-pressed'], true);
  assert.match(ui.text(ui.page()), /Published workflow revision 3.*Current plan dependency graph.*may differ after graph-only updates/s);
  const nodes = ui.all().filter(node => node.props['data-flowx-node']);
  assert.deepEqual(nodes.map(node => node.props['data-flowx-node']), ['Collect', 'Publish', 'Validate']);
  assert.deepEqual(ui.all().filter(node => node.props['data-flowx-edge']).map(node => node.props['data-flowx-edge']), ['Collect → Publish', 'Validate → Publish', 'Collect → Validate']);
  const x = Object.fromEntries(nodes.map(node => [node.props['data-flowx-node'], node.props.children.find(child => child.type === 'rect').props.x]));
  assert.ok(x.Collect < x.Validate && x.Validate < x.Publish);
  const disabled = ui.find(node => node.props['data-flowx-node'] === 'Publish');
  assert.match(disabled.props['aria-label'], /disabled/i);
  assert.equal(disabled.props.tabIndex, 0);
  assert.equal((await ui.key(disabled, ' ')).prevented, true);
  assert.match(ui.text(ui.page()), /Send it.*Status: Disabled · Depends on: Collect, Validate/s);
  const validate = ui.find(node => node.props['data-flowx-node'] === 'Validate');
  assert.equal((await ui.key(validate, 'Enter')).prevented, true);
  assert.match(ui.text(ui.page()), /Check input.*Status: Enabled · Depends on: Collect.*Node ID: node-v · Published node revision 2/s);
  assert.equal(ui.find(node => node.props['data-flowx-node'] === 'Validate').props['aria-pressed'], true);
  for (const [label, expectedPath, content] of [['markdown', 'nodes/validate.md', '# Validate'], ['Requirement', 'plan/requirement.md', '# Requirements'], ['Main entrypoint', 'main.py', 'print("main")'], ['Graph JSON', 'plan/graph.json', JSON.stringify(graph)]]) {
    await ui.click(ui.button(label));
    assert.deepEqual(calls.at(-1), { endpoint: 'readArtifact', payload: { workflowId: 'wf1', path: expectedPath } });
    const pre = ui.find(node => node.type === 'pre');
    assert.equal(ui.text(pre), content);
    assert.equal(pre.props.tabIndex, 0);
    assert.match(ui.text(ui.page()), new RegExp(`${content.length} bytes`));
  }
  ui.unmount();
});

test('invalid graphs and graph warnings are displayed while folder browsing remains available', async () => {
  let graphContent = '{invalid';
  const ui = harness((url, endpoint) => {
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap({ graph: 'graph.json' }) });
    if (endpoint === 'readArtifact') return ok({ content: graphContent });
    if (endpoint === 'listFolder') return ok(entries());
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('wf1'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /Unable to load graph/);
  assert.match(ui.text(ui.page()), /No visible files/);
  graphContent = JSON.stringify({ nodes: [{ name: 'a', depends: ['b', 'missing'] }, { name: 'b', depends: ['a'] }, { name: 'a' }, {}] });
  await ui.click(ui.workflow('wf1'));
  const warning = ui.text(ui.find(node => node.props.role === 'status' && ui.text(node).includes('missing node')));
  assert.match(warning, /missing node missing/);
  assert.match(warning, /cycle/);
  assert.match(warning, /Duplicate node/);
  assert.match(warning, /without a name/);
  assert.equal(ui.all().filter(node => node.props['data-flowx-node']).length, 2);
  ui.unmount();
});

test('nested folder browsing opens and closes subtrees, reads files and shows unavailable and truncated entries', async () => {
  const calls = [];
  const listings = {
    '': { entries: [entry('src', 'directory'), entry('image.png', 'file', false)], truncated: true },
    src: entries(entry('src/nested', 'directory'), entry('src/main.py')),
    'src/nested': entries(entry('src/nested/readme.md')),
  };
  const ui = harness((url, endpoint, payload) => {
    calls.push({ endpoint, payload });
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap() });
    if (endpoint === 'listFolder') return ok(listings[payload.path]);
    if (endpoint === 'readFolderFile') return ok({ content: 'hello folder', bytes: 12 });
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('wf1'));
  assert.match(ui.text(ui.page()), /No graph artifact is available/);
  assert.match(ui.text(ui.page()), /Folder listing is limited/);
  assert.equal(ui.path('image.png').props.disabled, true);
  assert.match(ui.text(ui.path('image.png')), /unavailable/);
  assert.equal(ui.path('src').props['aria-expanded'], false);
  await ui.click(ui.path('src'));
  assert.equal(ui.path('src').props['aria-expanded'], true);
  await ui.click(ui.path('src/nested'));
  await ui.click(ui.path('src/nested/readme.md'));
  assert.deepEqual(calls.at(-1), { endpoint: 'readFolderFile', payload: { workflowId: 'wf1', path: 'src/nested/readme.md' } });
  assert.equal(ui.text(ui.find(node => node.type === 'pre')), 'hello folder');
  assert.match(ui.text(ui.page()), /src\/nested\/readme.md · 12 bytes/);
  await ui.click(ui.path('src'));
  assert.equal(ui.path('src').props['aria-expanded'], false);
  assert.equal(ui.all().some(node => node.props['data-flowx-path'] === 'src/nested'), false);
  await ui.click(ui.path('src'));
  assert.equal(ui.path('src/nested').props['aria-expanded'], false);
  assert.equal(calls.filter(call => call.endpoint === 'listFolder' && call.payload.path === 'src').length, 2);
  ui.unmount();
});

test('folder errors can be retried and invalid or unavailable file responses are visible', async () => {
  let attempts = 0, reads = 0;
  const ui = harness((url, endpoint, payload) => {
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap({ requirement: 'req.md' }) });
    if (endpoint === 'listFolder') {
      attempts++;
      if (attempts === 1) return ok({ entries: null });
      if (attempts === 2) return fail('flowx/unavailable', 'Folder unavailable.');
      return ok(entries(entry('readme.md')));
    }
    if (endpoint === 'readFolderFile') return ++reads === 1 ? fail('flowx/unavailable', 'File unavailable.') : ok({ content: null });
    if (endpoint === 'readArtifact') return fail('flowx/unavailable', 'Artifact unavailable.');
    throw new Error(`${endpoint}: ${JSON.stringify(payload)}`);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('wf1'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /folder listing is invalid/i);
  await ui.click(ui.button('Retry folder'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /Folder unavailable/);
  await ui.click(ui.button('Retry folder'));
  await ui.click(ui.path('readme.md'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /File unavailable/);
  await ui.click(ui.path('readme.md'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /file response is invalid/i);
  await ui.click(ui.button('Requirement'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /Artifact unavailable/);
  ui.unmount();
});

test('settings load, validate on the host, save paths and refresh the catalog', async () => {
  const calls = [];
  let lists = 0, saves = 0;
  const ui = harness((url, endpoint, payload) => {
    calls.push({ endpoint, payload });
    if (endpoint === 'list') return ok(catalog(++lists === 1 ? 'old' : 'fresh'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap() });
    if (endpoint === 'listFolder') return ok(entries());
    if (endpoint === 'getSettings') return ok(settings);
    if (endpoint === 'saveSettings') return ++saves === 1 ? fail('flowx/configuration', 'FlowX settings must contain safe absolute paths.') : ok(payload);
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('old'));
  await ui.click(ui.button('Settings'));
  const dialog = ui.find(node => node.props.role === 'dialog');
  assert.equal(dialog.props['aria-modal'], true);
  for (const [key, label] of [['python', 'FLOWX_PYTHON'], ['workspace', 'FLOWX_WORKSPACE'], ['apiKey', 'FLOWX_LLM_API_KEY']]) {
    assert.equal(ui.input(key).props.value, settings[key]);
    assert.equal(ui.input(key).props.required, key !== 'apiKey');
    assert.ok(ui.find(node => node.type === 'label' && node.props.htmlFor === `flowx-${key}` && ui.text(node) === label));
  }
  await ui.change('workspace', 'relative');
  await ui.submit();
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /safe absolute paths/);
  assert.equal(ui.input('workspace').props.value, 'relative');
  await ui.change('workspace', '/new-workspace');
  await ui.submit();
  assert.deepEqual(calls.filter(call => call.endpoint === 'saveSettings').map(call => call.payload), [
    { ...settings, workspace: 'relative' }, { ...settings, workspace: '/new-workspace' },
  ]);
  assert.equal(ui.all().some(node => node.props.role === 'dialog'), false);
  assert.equal(ui.all().some(node => node.props.id === 'flowx-explorer-heading'), false);
  assert.ok(ui.workflow('fresh'));
  assert.equal(lists, 2);
  assert.equal(ui.focus.at(-1), 'Settings');
  ui.unmount();
});

test('settings loading failures and cancel preserve the catalog and discard edits', async () => {
  let gets = 0, saves = 0;
  const ui = harness((url, endpoint) => {
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'getSettings') return ++gets === 1 ? fail('flowx/settings', 'Settings unavailable.') : ok({ ...settings, python: null });
    if (endpoint === 'saveSettings') { saves++; return ok(settings); }
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.button('Settings'));
  assert.match(ui.text(ui.find(node => node.props.role === 'alert')), /Settings unavailable/);
  await ui.click(ui.button('Cancel'));
  assert.ok(ui.workflow('wf1'));
  await ui.click(ui.button('Settings'));
  assert.equal(ui.input('python').props.value, '');
  await ui.change('workspace', '/unsaved');
  await ui.click(ui.button('Cancel'));
  await ui.click(ui.button('Settings'));
  assert.equal(ui.input('workspace').props.value, settings.workspace);
  assert.equal(saves, 0);
  ui.unmount();
});

test('Escape cancels settings first, then closes the overlay and restores launcher focus', async () => {
  const ui = harness((url, endpoint) => endpoint === 'list' ? ok(catalog()) : ok(settings));
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.button('Settings'));
  assert.equal((await ui.key(ui.page(), 'x')).stopped, false);
  assert.equal((await ui.key(ui.page(), 'Escape')).stopped, true);
  assert.equal(ui.all().some(node => node.props.role === 'dialog'), false);
  assert.equal(ui.launcher().props['aria-expanded'], true);
  assert.equal(ui.focus.at(-1), 'Settings');
  assert.equal((await ui.key(ui.page(), 'Escape')).stopped, true);
  assert.equal(ui.overlay(), null);
  assert.equal(ui.focus.at(-1), 'launcher');
  ui.unmount();
});

test('a late settings load cannot reopen a cancelled dialog or overwrite a later load', async () => {
  const old = deferred();
  let gets = 0;
  const ui = harness((url, endpoint) => {
    if (endpoint === 'list') return ok(catalog());
    if (endpoint === 'getSettings') return ++gets === 1 ? old.promise : ok({ ...settings, workspace: '/new' });
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.button('Settings'));
  assert.equal(ui.input('workspace').props.disabled, true);
  assert.equal(ui.button('Cancel').props.disabled, false);
  await ui.key(ui.page(), 'Escape');
  await ui.click(ui.button('Settings'));
  assert.equal(ui.input('workspace').props.value, '/new');
  old.resolve(ok({ ...settings, workspace: '/old' }));
  await ui.flush();
  assert.equal(ui.input('workspace').props.value, '/new');
  ui.unmount();
});

test('the latest overlapping catalog refresh wins even when an older response arrives last', async () => {
  const first = deferred(), second = deferred();
  let lists = 0;
  const ui = harness((url, endpoint) => {
    assert.equal(endpoint, 'list');
    lists++;
    return lists === 1 ? ok(catalog('initial')) : lists === 2 ? first.promise : second.promise;
  });
  await ui.mount();
  await ui.toggle();
  const refresh = ui.button('Refresh');
  ui.fire(refresh);
  ui.fire(refresh); // Two clicks before React has committed the disabled state.
  await ui.flush();
  assert.equal(lists, 3);
  second.resolve(ok(catalog('new')));
  await ui.flush();
  first.resolve(fail('flowx/old', 'Stale catalog failure.'));
  await ui.flush();
  assert.ok(ui.workflow('new'));
  assert.equal(ui.all().some(node => node.props.role === 'alert'), false);
  ui.unmount();
});

test('changing the selected file ignores an older artifact or folder-file response', async () => {
  const old = deferred();
  const ui = harness((url, endpoint, payload) => {
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap({ requirement: 'req.md' }) });
    if (endpoint === 'listFolder') return ok(entries(entry('new.txt')));
    if (endpoint === 'readArtifact') return old.promise;
    if (endpoint === 'readFolderFile') return ok({ content: 'new content', bytes: 11 });
    throw new Error(`${endpoint} ${JSON.stringify(payload)}`);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('wf1'));
  await ui.click(ui.button('Requirement'));
  assert.match(ui.text(ui.page()), /Loading file/);
  await ui.click(ui.path('new.txt'));
  old.resolve(ok({ content: 'old content', bytes: 11 }));
  await ui.flush();
  assert.equal(ui.text(ui.find(node => node.type === 'pre')), 'new content');
  assert.doesNotMatch(ui.text(ui.page()), /old content/);
  ui.unmount();
});

test('workflow selection discards late file and folder responses from the previous workflow', async () => {
  const oldFile = deferred(), oldFolder = deferred();
  const ui = harness((url, endpoint, payload) => {
    if (endpoint === 'list') return ok(catalog('old', 'new'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap({ requirement: `${payload.workflowId}.md` }) });
    if (endpoint === 'listFolder') return payload.workflowId === 'old' ? oldFolder.promise : ok(entries(entry('new.txt')));
    if (endpoint === 'readArtifact') return payload.workflowId === 'old' ? oldFile.promise : ok({ content: 'new artifact' });
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('old'));
  await ui.click(ui.button('Requirement'));
  await ui.click(ui.workflow('new'));
  await ui.click(ui.button('Requirement'));
  oldFolder.resolve(ok(entries(entry('old.txt'))));
  oldFile.resolve(fail('flowx/old', 'Old file error.'));
  await ui.flush();
  assert.equal(ui.workflow('new').props['aria-pressed'], true);
  assert.ok(ui.path('new.txt'));
  assert.equal(ui.all().some(node => node.props['data-flowx-path'] === 'old.txt'), false);
  assert.equal(ui.text(ui.find(node => node.type === 'pre')), 'new artifact');
  assert.equal(ui.all().some(node => node.props.role === 'alert'), false);
  ui.unmount();
});

test('refresh clears the explorer and ignores late folder and file responses', async () => {
  const oldFile = deferred(), oldFolder = deferred();
  const ui = harness((url, endpoint) => {
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap({ requirement: 'req.md' }) });
    if (endpoint === 'listFolder') return oldFolder.promise;
    if (endpoint === 'readArtifact') return oldFile.promise;
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('wf1'));
  await ui.click(ui.button('Requirement'));
  await ui.click(ui.button('Refresh'));
  oldFolder.resolve(ok(entries(entry('stale.txt'))));
  oldFile.resolve(ok({ content: 'stale content' }));
  await ui.flush();
  assert.ok(ui.workflow('wf1'));
  assert.equal(ui.all().some(node => node.props.id === 'flowx-explorer-heading'), false);
  assert.doesNotMatch(ui.text(ui.page()), /stale/);
  ui.unmount();
});

test('a late inspection cannot restore a workflow after a refresh', async () => {
  const inspection = deferred();
  let graphReads = 0, folderReads = 0;
  const ui = harness((url, endpoint) => {
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return inspection.promise;
    if (endpoint === 'readArtifact') { graphReads++; return ok({ content: '{"nodes":[]}' }); }
    if (endpoint === 'listFolder') { folderReads++; return ok(entries()); }
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  const refresh = ui.button('Refresh');
  ui.fire(ui.workflow('wf1'));
  ui.fire(refresh); // Both events precede the disabled-state commit.
  await ui.flush();
  inspection.resolve(ok({ artifacts: artifactMap({ graph: 'old.json' }) }));
  await ui.flush();
  assert.ok(ui.workflow('wf1'));
  assert.equal(ui.all().some(node => node.props.id === 'flowx-explorer-heading'), false);
  assert.equal(graphReads, 0);
  assert.equal(folderReads, 0);
  ui.unmount();
});

test('closing and reopening the overlay isolates a late inspection from the new page', async () => {
  const inspection = deferred();
  const calls = [];
  const ui = harness((url, endpoint, payload) => {
    calls.push({ endpoint, payload });
    if (endpoint === 'list') return ok(catalog('old', 'new'));
    if (endpoint === 'inspect') return payload.workflowId === 'old' ? inspection.promise : ok({ artifacts: artifactMap() });
    if (endpoint === 'listFolder') return ok(entries(entry('new.txt')));
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('old'));
  assert.match(ui.text(ui.page()), /Loading workflow and graph/);
  await ui.click(ui.button('Close'));
  await ui.toggle();
  await ui.click(ui.workflow('new'));
  inspection.resolve(ok({ artifacts: artifactMap({ graph: 'old.json' }) }));
  await ui.flush();
  assert.equal(ui.workflow('new').props['aria-pressed'], true);
  assert.ok(ui.path('new.txt'));
  assert.equal(calls.some(call => call.endpoint === 'readArtifact'), false);
  assert.equal(calls.filter(call => call.endpoint === 'listFolder').length, 1);
  ui.unmount();
});

test('closing and reopening a folder ignores its earlier in-flight listing', async () => {
  const old = deferred();
  let folderRequests = 0;
  const ui = harness((url, endpoint, payload) => {
    if (endpoint === 'list') return ok(catalog('wf1'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap() });
    if (endpoint === 'listFolder' && payload.path === '') return ok(entries(entry('src', 'directory')));
    if (endpoint === 'listFolder') return ++folderRequests === 1 ? old.promise : ok(entries(entry('src/new.py')));
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('wf1'));
  await ui.click(ui.path('src'));
  assert.match(ui.text(ui.page()), /Loading folder/);
  await ui.click(ui.path('src'));
  await ui.click(ui.path('src'));
  old.resolve(ok(entries(entry('src/old.py'))));
  await ui.flush();
  assert.ok(ui.path('src/new.py'));
  assert.equal(ui.all().some(node => node.props['data-flowx-path'] === 'src/old.py'), false);
  ui.unmount();
});

test('saving settings refreshes and invalidates pending explorer responses', async () => {
  const oldFile = deferred(), oldFolder = deferred(), save = deferred();
  let lists = 0;
  const ui = harness((url, endpoint) => {
    if (endpoint === 'list') return ok(catalog(++lists === 1 ? 'old' : 'fresh'));
    if (endpoint === 'inspect') return ok({ artifacts: artifactMap({ requirement: 'req.md' }) });
    if (endpoint === 'readArtifact') return oldFile.promise;
    if (endpoint === 'listFolder') return oldFolder.promise;
    if (endpoint === 'getSettings') return ok(settings);
    if (endpoint === 'saveSettings') return save.promise;
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.workflow('old'));
  await ui.click(ui.button('Requirement'));
  await ui.click(ui.button('Settings'));
  await ui.submit();
  assert.equal(ui.button('Working…').props.disabled, true);
  save.resolve(ok(settings));
  await ui.flush();
  oldFolder.resolve(ok(entries(entry('old.txt'))));
  oldFile.resolve(ok({ content: 'old content' }));
  await ui.flush();
  assert.ok(ui.workflow('fresh'));
  assert.equal(ui.all().some(node => node.props.id === 'flowx-explorer-heading'), false);
  assert.doesNotMatch(ui.text(ui.page()), /old content|old.txt/);
  ui.unmount();
});

test('unmounting discards delayed catalog and settings responses', async () => {
  const list = deferred(), get = deferred();
  let calls = 0;
  const ui = harness((url, endpoint) => {
    calls++;
    if (endpoint === 'list') return list.promise;
    if (endpoint === 'getSettings') return get.promise;
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.button('Settings'));
  await ui.click(ui.button('Close'));
  list.resolve(ok(catalog('stale')));
  get.resolve(ok(settings));
  await ui.flush();
  assert.equal(ui.overlay(), null);
  assert.equal(ui.page(), undefined);
  assert.equal(calls, 2);
  ui.unmount();
});

test('a successful save still refreshes the catalog if Escape is pressed while saving', async () => {
  const save = deferred();
  let lists = 0;
  const ui = harness((url, endpoint) => {
    if (endpoint === 'list') return ok(catalog(++lists === 1 ? 'old' : 'fresh'));
    if (endpoint === 'getSettings') return ok(settings);
    if (endpoint === 'saveSettings') return save.promise;
    throw new Error(endpoint);
  });
  await ui.mount();
  await ui.toggle();
  await ui.click(ui.button('Settings'));
  await ui.change('workspace', '/new-workspace');
  await ui.submit();
  await ui.key(ui.page(), 'Escape');
  save.resolve(ok({ ...settings, workspace: '/new-workspace' }));
  await ui.flush();
  assert.equal(lists, 2, 'the persisted workspace change must trigger a catalog refresh');
  assert.ok(ui.workflow('fresh'));
  ui.unmount();
});