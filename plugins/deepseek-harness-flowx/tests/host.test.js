// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: exercise the DeepSeek Harness host without external services.
// Modified 2026-11-09: cover host schema, secret handling and bounded results.
// Modified 2026-11-09: validate the shared Fetch route and its authorization boundary.

import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough, Writable } from 'node:stream';
import * as fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { FlowXHostService } from '../host/service.js';
import { apply, inject, name } from '../index.js';

const OWNER = { principal: { kind: 'owner' } };
const LIMIT = 1024 * 1024;
const BUILD_EVENT_LIMIT = 16 * LIMIT;
const call = (service, endpoint, payload = null, peer = OWNER) => service.dispatch(endpoint, payload, peer);
const value = async (...args) => { const result = await call(...args); assert.equal(result.ok, true, JSON.stringify(result)); return result.value; };
const waitFor = async (predicate, message = 'condition') => {
  const until = Date.now() + 5000;
  while (Date.now() < until) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail(`Timed out waiting for ${message}`);
};

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'flowx-host-test-'));
  const workspace = path.join(root, 'workspace');
  const skill = path.join(root, 'skill');
  await fs.mkdir(path.join(skill, 'scripts'), { recursive: true });
  await fs.mkdir(workspace);
  await fs.writeFile(path.join(skill, 'scripts', 'build.py'), '');
  await fs.writeFile(path.join(skill, 'scripts', 'inspect_workflow.py'), '');
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return { root, workspace, skill, env: { ...process.env, FLOWX_WORKSPACE: workspace, FLOWX_PYTHON: process.execPath, FLOWX_SKILL_DIR: skill, FLOWX_LLM_API_KEY: 'test-secret-value' } };
}

class FakeChild extends EventEmitter {
  constructor() {
    super();
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.writes = [];
    this.stdin = new Writable({ write: (chunk, _encoding, callback) => { this.writes.push(String(chunk)); callback(); } });
    this.exited = false;
  }
  emitJson(event) { this.stdout.write(JSON.stringify(event) + '\n'); }
  exit(code = 0, signal = null) { if (!this.exited) { this.exited = true; this.emit('close', code, signal); } }
  kill(signal) { this.exit(null, signal); return true; }
}

function fakeSpawn(inspection = () => ({ ok: false })) {
  const calls = [];
  const spawn = (command, args, options) => {
    const child = new FakeChild();
    calls.push({ command, args, options, child });
    if (args[0].endsWith('inspect_workflow.py')) {
      setImmediate(() => {
        const output = inspection(args);
        if (output === null) return;
        child.stdout.write(typeof output === 'string' ? output : JSON.stringify(output));
        child.exit(0);
      });
    }
    return child;
  };
  return { calls, spawn };
}

async function startFake(service, fake, prompt = 'make a workflow') {
  await value(service, 'start', { prompt });
  const child = fake.calls.at(-1).child;
  child.emitJson({ event: 'allocated', workflow_id: 'wf1' });
  child.emitJson({ event: 'started', workflow_id: 'wf1', operation: 'new' });
  return child;
}

function pause(child, prompt = 'fix it', testLog = 'failed') {
  child.emitJson({ event: 'input_required', workflow_id: 'wf1', request_id: 'r1', awaiting: { stepId: 'node__repair', prompt, test_log: testLog } });
}

function mountRoute() {
  let route, cleanup;
  const ctx = {
    connection: { fetch: { register(options) { assert.equal(route, undefined); route = options; } } },
    effect(fn) { cleanup = fn(); },
  };
  apply(ctx);
  return { route, cleanup };
}

const request = (body, headers = { 'content-type': 'application/json' }) =>
  new Request('http://localhost/api/flowx', { method: 'POST', headers, body });
const jsonRequest = body => request(JSON.stringify(body));

test('Cordis registers the exact buffered POST route and disposes its service', async () => {
  assert.equal(name, 'deepseek-harness-flowx');
  assert.deepEqual(inject, ['connection']);
  const { route, cleanup } = mountRoute();
  assert.equal(route.path, '/api/flowx');
  assert.deepEqual(route.methods, ['POST']);
  assert.equal(route.requestBody, 'buffered');
  const response = await route.fetch(jsonRequest({ endpoint: 'status', payload: null }));
  assert.equal(response.status, 200);
  assert.match(response.headers.get('content-type'), /^application\/json\b/);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal((await response.json()).value.status, 'idle');
  await cleanup();
  assert.equal((await (await route.fetch(jsonRequest({ endpoint: 'status', payload: null }))).json()).error.code, 'flowx/disposed');
});

test('route dispatches as owner only after Harness exact-route admission', async t => {
  const seen = [];
  t.mock.method(FlowXHostService.prototype, 'dispatch', async (endpoint, payload, peer) => {
    seen.push({ endpoint, payload, peer });
    return { ok: true, value: 'accepted' };
  });
  const { route, cleanup } = mountRoute();
  t.after(cleanup);
  // This simulates the documented carrier boundary, not authentication itself:
  // Harness rejects unauthenticated and non-owner callers before route.fetch.
  const admit = (req, principal, authenticated = true) => {
    if (!authenticated) return new Response('unauthorized', { status: 401 });
    if (new URL(req.url).pathname !== route.path || !route.methods.includes(req.method)) return new Response('not found', { status: 404 });
    if (principal !== 'owner') return new Response('forbidden', { status: 403 });
    return route.fetch(req);
  };
  assert.equal((await admit(jsonRequest({ endpoint: 'status', payload: null }), 'owner', false)).status, 401);
  assert.equal((await admit(jsonRequest({ endpoint: 'status', payload: null }), 'guest')).status, 403);
  assert.equal((await admit(new Request('http://localhost/api/flowx', { method: 'GET' }), 'owner')).status, 404);
  assert.equal((await admit(new Request('http://localhost/api/flowx/extra', { method: 'POST' }), 'owner')).status, 404);
  assert.equal(seen.length, 0);
  const result = await admit(jsonRequest({ endpoint: 'inspect', payload: { workflowId: 'wf1' } }), 'owner');
  assert.deepEqual(await result.json(), { ok: true, value: 'accepted' });
  assert.deepEqual(seen, [{ endpoint: 'inspect', payload: { workflowId: 'wf1' }, peer: OWNER }]);
});

test('route validates content type, UTF-8, JSON envelope, and actual byte limit', async t => {
  const { route, cleanup } = mountRoute();
  t.after(cleanup);
  for (const contentType of [undefined, 'text/plain', 'application/jsonp', 'application/json-seq']) {
    const body = new TextEncoder().encode('{"endpoint":"status","payload":null}');
    const response = await route.fetch(request(body, contentType ? { 'content-type': contentType } : {}));
    assert.equal(response.status, 415);
    assert.equal((await response.json()).error.code, 'flowx/unsupported-media-type');
  }
  for (const contentType of ['application/json', 'Application/JSON; charset=utf-8']) {
    const response = await route.fetch(request('{"endpoint":"status","payload":null}', { 'content-type': contentType }));
    assert.equal((await response.json()).value.status, 'idle');
  }
  for (const body of ['', '{', 'null', '[]', '1', '"text"', '{}', '{"endpoint":"status"}', '{"payload":null}', '{"endpoint":1,"payload":null}', '{"endpoint":"status","payload":null,"extra":true}']) {
    const response = await route.fetch(request(body));
    assert.equal(response.status, 400, body);
    assert.equal((await response.json()).error.code, 'flowx/invalid-request');
  }
  const invalidUtf8 = await route.fetch(request(new Uint8Array([0xff])));
  assert.equal(invalidUtf8.status, 400);
  const valid = '{"endpoint":"status","payload":null}';
  const exact = valid + ' '.repeat(LIMIT - Buffer.byteLength(valid));
  assert.equal((await route.fetch(request(exact))).status, 200);
  const oversized = await route.fetch(request(exact + ' ', { 'content-type': 'application/json', 'content-length': '1' }));
  assert.equal(oversized.status, 413);
  assert.equal((await oversized.json()).error.code, 'flowx/request-too-large');
  const multibyte = await route.fetch(request('é'.repeat(LIMIT / 2 + 1)));
  assert.equal(multibyte.status, 413);
  const unknown = await route.fetch(jsonRequest({ endpoint: 'missing', payload: null }));
  assert.equal((await unknown.json()).error.code, 'flowx/unknown-endpoint');
  const invalidPayload = await route.fetch(jsonRequest({ endpoint: 'status', payload: {} }));
  assert.equal((await invalidPayload.json()).error.code, 'flowx/invalid-payload');
});

test('route never exposes unexpected body or dispatch errors', async t => {
  t.mock.method(FlowXHostService.prototype, 'dispatch', async () => { throw new Error('private dispatch secret'); });
  const { route, cleanup } = mountRoute();
  t.after(cleanup);
  const badBody = { headers: new Headers({ 'content-type': 'application/json' }), arrayBuffer: async () => { throw new Error('private body secret'); } };
  for (const req of [badBody, jsonRequest({ endpoint: 'status', payload: null })]) {
    const response = await route.fetch(req);
    assert.equal(response.status, 500);
    assert.deepEqual(await response.json(), { ok: false, error: { code: 'flowx/internal', message: 'The operation failed.', details: {} } });
  }
});

test('host service independently rejects non-owner peers', async () => {
  const service = new FlowXHostService();
  for (const peer of [undefined, {}, { principal: { kind: 'guest' } }, { kind: 'owner' }]) {
    assert.deepEqual((await service.dispatch('status', null, peer)).error, { code: 'flowx/forbidden', message: 'Only the owner may use FlowX.', details: {} });
  }
  assert.equal((await service.dispatch('status', null, OWNER)).value.status, 'idle');
  await service.dispose();
});

test('all RPC errors include the Harness details object', async () => {
  const service = new FlowXHostService();
  assert.deepEqual((await call(service, 'status', {})).error, { code: 'flowx/invalid-payload', message: 'Invalid endpoint payload.', details: {} });
  service.list = () => { throw new Error('private data'); };
  assert.deepEqual((await call(service, 'list')).error, { code: 'flowx/internal', message: 'The operation failed.', details: {} });
  await service.dispose();
  assert.deepEqual((await call(service, 'status')).error, { code: 'flowx/disposed', message: 'The plugin has been disposed.', details: {} });
});

test('revisions are monotonic within a unique service instance', async t => {
  const { env } = await fixture(t);
  const first = new FlowXHostService({ env });
  const second = new FlowXHostService({ env });
  const initial = await value(first, 'status');
  const other = await value(second, 'status');
  assert.match(initial.instanceId, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(initial.instanceId, other.instanceId);
  assert.equal(initial.revision, other.revision);
  first.update({ status: 'running' });
  const later = await value(first, 'status');
  assert.equal(later.instanceId, initial.instanceId);
  assert.equal(later.revision, initial.revision + 1);
  await first.dispose();
  assert.equal(first.snapshot().instanceId, initial.instanceId);
  await second.dispose();
});

test('child environment retains Python and LLM configuration but drops unrelated credentials', async t => {
  const { env } = await fixture(t);
  Object.assign(env, {
    PYTHONPATH: '/python/modules', VIRTUAL_ENV: '/python/venv', OPENAI_BASE_URL: 'https://example.test/v1',
    FLOWX_LLM_PROVIDER: 'qwen', DASHSCOPE_API_KEY: 'provider-key', OPENAI_API_KEY: 'openai-key',
    GOOGLE_APPLICATION_CREDENTIALS: '/home/user/credential.json', AWS_CONFIG_FILE: '/home/user/aws-config',
    GITHUB_TOKEN: 'github-token', AWS_SECRET_ACCESS_KEY: 'aws-secret', AWS_ACCESS_KEY_ID: 'aws-id',
    ANTHROPIC_API_KEY: 'unrelated-key', NPM_CONFIG_PASSWORD: 'npm-password', CI_JOB_JWT: 'job-jwt',
    DATABASE_URL: 'postgres://dbuser:dbpass@example.test/db', HTTPS_PROXY: 'http://user:pass@proxy.test',
  });
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  const child = await startFake(service, fake);
  const inherited = fake.calls[0].options.env;
  for (const key of ['PATH', 'HOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'OPENAI_BASE_URL', 'FLOWX_LLM_PROVIDER', 'FLOWX_LLM_API_KEY', 'DASHSCOPE_API_KEY', 'OPENAI_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'AWS_CONFIG_FILE', 'HTTPS_PROXY']) assert.equal(inherited[key], env[key], key);
  for (const key of ['GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'AWS_ACCESS_KEY_ID', 'ANTHROPIC_API_KEY', 'NPM_CONFIG_PASSWORD', 'CI_JOB_JWT', 'DATABASE_URL']) assert.equal(Object.hasOwn(inherited, key), false, key);
  assert.equal(env.GITHUB_TOKEN, 'github-token');
  assert.doesNotMatch(service.redact('dbpass user pass dbuser'), /dbpass|dbuser|\buser\b|\bpass\b/);
  child.exit(1);
  await waitFor(() => !service.active);
});

test('repair output is bounded after redaction and handles short overlapping secrets', async t => {
  const { env } = await fixture(t);
  Object.assign(env, { CUSTOM_TOKEN: 'abc', CUSTOM_PASSWORD: 'ab', ANOTHER_SECRET: 'REDACTED' });
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  const child = await startFake(service, fake);
  pause(child, 'abc ab ' + 'x'.repeat(LIMIT + 10), 'password="unknown secret with spaces');
  const snapshot = await value(service, 'status');
  assert.equal(snapshot.status, 'input_required');
  assert.match(snapshot.awaiting.prompt, /^\[REDACTED\] \[REDACTED\]/);
  assert.match(snapshot.awaiting.prompt, /\[truncated\]$/);
  assert.equal(snapshot.awaiting.test_log, 'password=[REDACTED]');
  assert.ok(Buffer.byteLength(JSON.stringify(snapshot)) < 10000);
  child.exit(1);
  await waitFor(() => !service.active);
});

test('strict validation, process-wide lease, repair revision and duplicate protection', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  const other = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(async () => { await service.dispose(); await other.dispose(); });
  for (const [endpoint, payload] of [['status', {}], ['list', undefined], ['start', { prompt: '' }], ['start', { prompt: 'x', extra: 1 }], ['start', { prompt: 'x'.repeat(LIMIT + 1) }], ['inspect', { workflowId: '../x' }], ['readArtifact', { workflowId: 'wf1', path: '../x' }]]) {
    // Explicit undefined is not null for the transport contract.
    const result = await service.dispatch(endpoint, payload, OWNER);
    assert.equal(result.error.code, 'flowx/invalid-payload');
  }
  assert.equal((await call(service, 'no-such-method')).error.code, 'flowx/unknown-endpoint');
  const child = await startFake(service, fake);
  assert.equal(fake.calls[0].options.shell, false);
  assert.deepEqual(fake.calls[0].args.slice(-4, -2), ['--host', '127.0.0.1']);
  assert.equal((await call(other, 'start', { prompt: 'x' })).error.code, 'flowx/busy');
  assert.equal((await call(service, 'list')).error.code, 'flowx/busy');
  pause(child, 'prompt test-secret-value', 'token=abc test-secret-value');
  const paused = await value(service, 'status');
  assert.equal(paused.status, 'input_required');
  assert.equal(paused.workflowId, 'wf1');
  assert.doesNotMatch(JSON.stringify(paused), /test-secret-value|token=abc/);
  const answer = { requestId: 'r1', stepId: 'node__repair', revision: paused.revision, input: '' };
  assert.equal((await call(service, 'answer', { ...answer, revision: paused.revision - 1 })).error.code, 'flowx/stale-input');
  assert.equal((await call(service, 'answer', { ...answer, stepId: 'wrong' })).error.code, 'flowx/stale-input');
  assert.equal((await value(service, 'answer', answer)).status, 'running');
  assert.deepEqual(JSON.parse(child.writes[0]), { action: 'answer', step_id: 'node__repair', input: '' });
  assert.equal((await call(service, 'answer', answer)).error.code, 'flowx/stale-input');
  pause(child);
  assert.equal((await call(service, 'answer', answer)).error.code, 'flowx/stale-input');
  const again = await value(service, 'status');
  const abort = { requestId: again.requestId, stepId: again.awaiting.stepId, revision: again.revision };
  assert.equal((await value(service, 'abort', abort)).status, 'aborting');
  assert.deepEqual(JSON.parse(child.writes[1]), { action: 'abort' });
  assert.equal((await call(service, 'abort', abort)).error.code, 'flowx/stale-input');
  child.emitJson({ event: 'aborted', workflow_id: 'wf1', reason: 'requested' });
  child.exit(1);
  await waitFor(() => service.state.status === 'aborted');
});

test('completion requires successful result and clean process exit; failures are sanitized', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  for (const scenario of [
    { event: { event: 'result', result: { workflow_id: 'wf1', ok: false, status: 'completed' } }, code: 1 },
    { event: { event: 'result', result: { workflow_id: 'wf1', ok: true, status: 'input_required' } }, code: 0 },
    { event: { event: 'result', result: { workflow_id: 'wf1', ok: true, status: 'completed' } }, code: 1 },
    { event: { event: 'error', error: 'test-secret-value' }, code: 1 },
    { event: null, code: 0 },
  ]) {
    const child = await startFake(service, fake);
    child.stderr.write('test-secret-value');
    if (scenario.event) child.emitJson(scenario.event);
    child.exit(scenario.code);
    await waitFor(() => service.state.status === 'failed' && !service.active);
    assert.doesNotMatch(JSON.stringify(service.snapshot()), /test-secret-value/);
  }
  const child = await startFake(service, fake);
  child.emitJson({ event: 'result', result: { workflow_id: 'wf1', ok: true, status: 'completed', artifacts: null } });
  assert.notEqual(service.state.status, 'completed');
  child.exit(0);
  await waitFor(() => service.state.status === 'completed' && !service.active);
  assert.deepEqual(service.state.result, { ok: true, status: 'completed', workflowId: 'wf1' });
});

test('malformed and incomplete JSON-lines fail closed', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  for (const data of ['not json\n', '{"event":"allocated"}']) {
    const child = await startFake(service, fake);
    child.stdout.write(data);
    child.exit(0);
    await waitFor(() => service.state.status === 'failed' && !service.active);
  }
});

test('large terminal results are summarized without leaking errors or logs', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  const child = await startFake(service, fake);
  const result = {
    workflow_id: 'wf1', ok: false, status: 'completed', artifacts: null,
    stages: { graph: { status: 'completed', attempts: 1 }, nodes: { status: 'failed', attempts: 3, error: 'password=unknown-secret' }, main: { status: 'blocked' }, extra: { status: 'failed', error: 'unknown-secret' } },
    errors: { graph: 'test-secret-value', 'nodes/SomeNode': 'unknown-secret', 'secret-name': 'private' },
    logs: { output: 'test-secret-value ' + 'x'.repeat(LIMIT + 100) },
  };
  child.emitJson({ event: 'result', result });
  child.exit(1);
  await waitFor(() => service.state.status === 'failed' && !service.active);
  const snapshot = await value(service, 'status');
  assert.deepEqual(snapshot.result, {
    ok: false, status: 'completed', workflowId: 'wf1',
    stages: { graph: { status: 'completed', hasError: false, attempts: 1 }, nodes: { status: 'failed', hasError: true, attempts: 3 }, main: { status: 'blocked', hasError: false } },
    errors: { counts: { reformat: 0, graph: 1, nodes: 1, main: 0, other: 1 }, details: 'Withheld.' },
  });
  assert.doesNotMatch(JSON.stringify(snapshot), /test-secret-value|unknown-secret|secret-name|private/);
  assert.ok(Buffer.byteLength(JSON.stringify(snapshot)) < 4096);
  const success = await startFake(service, fake);
  success.emitJson({ event: 'result', result: { ...result, ok: true } });
  success.exit(0);
  await waitFor(() => service.state.status === 'completed' && !service.active);
  assert.equal(service.state.result.stages.nodes.status, 'failed');
  assert.ok(Buffer.byteLength(JSON.stringify(service.snapshot())) < 4096);
});

test('events above the build limit report an unknown outcome and retain candidate id', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  const child = await startFake(service, fake);
  child.stdout.write('x'.repeat(BUILD_EVENT_LIMIT));
  child.stdout.write('x');
  await waitFor(() => service.state.status === 'unknown' && !service.active);
  const snapshot = await value(service, 'status');
  assert.equal(snapshot.workflowId, 'wf1');
  assert.equal(snapshot.error.code, 'flowx/build-uncertain');
  assert.match(snapshot.error.message, /unknown.*candidate workflow/i);
  assert.equal(snapshot.result, null);
  assert.ok(Buffer.byteLength(JSON.stringify(snapshot)) < 4096);
});

test('configuration is absolute and errors do not reveal paths or credentials', async t => {
  const { env } = await fixture(t);
  for (const changes of [{ FLOWX_WORKSPACE: 'relative' }, { FLOWX_PYTHON: 'node' }, { FLOWX_SKILL_DIR: 'relative' }, { FLOWX_WORKSPACE: '/missing-test-secret-value' }]) {
    const service = new FlowXHostService({ env: { ...env, ...changes } });
    const result = await call(service, 'start', { prompt: 'secret' });
    assert.equal(result.error.code, 'flowx/configuration');
    assert.doesNotMatch(JSON.stringify(result), /secret-value/);
    await service.dispose();
  }
});

test('configured interpreter symlinks are executed as given, preserving virtualenv identity', async t => {
  const { env, root } = await fixture(t);
  const interpreter = path.join(root, 'python');
  await fs.symlink(process.execPath, interpreter);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env: { ...env, FLOWX_PYTHON: interpreter }, spawn: fake.spawn });
  t.after(() => service.dispose());
  const child = await startFake(service, fake);
  assert.equal(fake.calls[0].command, interpreter);
  child.exit(1);
  await waitFor(() => service.state.status === 'failed' && !service.active);
});

test('inspection preserves the complete nested artifact map and confines reads', async t => {
  const { env, workspace, root } = await fixture(t);
  const directory = path.join(workspace, 'workflows', 'wf1');
  await fs.mkdir(path.join(directory, 'plan'), { recursive: true });
  await fs.mkdir(path.join(directory, 'nodes'));
  await fs.writeFile(path.join(directory, 'plan', 'workflow.json'), '{"nodes":[]}');
  await fs.writeFile(path.join(directory, 'plan', 'requirement.md'), 'test-secret-value');
  await fs.writeFile(path.join(directory, 'nodes', 'node.py'), 'print("hello")');
  await fs.writeFile(path.join(root, 'outside.py'), 'private');
  await fs.symlink(path.join(root, 'outside.py'), path.join(directory, 'nodes', 'escape.py'));
  const map = {
    workflow_id: 'wf1', workflow_version: 3,
    graph: path.join(directory, 'plan', 'workflow.json'), requirement: path.join(directory, 'plan', 'requirement.md'),
    nodes: { First: { node_id: 'node1', version: 2, artifacts: { source: path.join(directory, 'nodes', 'node.py') } } },
    main_entrypoint: { id: 'm1', version: 1, path: null },
  };
  const fake = fakeSpawn(args => args[3] === 'list'
    ? { ok: true, operation: 'list', data: { wf1: { nodes: {}, workflow_version: 3, main_entrypoint_id: 'm1', errors: { graph: 'test-secret-value' }, has_graph: true, has_main: false } } }
    : { ok: true, operation: 'artifacts', data: map });
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  assert.equal((await value(service, 'list')).workflows.wf1.errors.graph, 'Details withheld.');
  assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: 'nodes/node.py' })).error.code, 'flowx/artifact-unavailable');
  const result = (await value(service, 'inspect', { workflowId: 'wf1' })).artifacts;
  assert.deepEqual(result, { ...map, graph: 'plan/workflow.json', requirement: 'plan/requirement.md', nodes: { First: { node_id: 'node1', version: 2, artifacts: { source: 'nodes/node.py' } } } });
  assert.equal((await value(service, 'readArtifact', { workflowId: 'wf1', path: 'nodes/node.py' })).content, 'print("hello")');
  assert.equal((await value(service, 'readArtifact', { workflowId: 'wf1', path: 'plan/requirement.md' })).content, '[REDACTED]');
  for (const candidate of ['/etc/passwd', '../outside.py', 'nodes/../plan/workflow.json', 'nodes\\node.py', 'nodes/escape.py']) {
    assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: candidate })).ok, false);
  }
  // Replacing a previously inspected file with a symlink is rejected at read time.
  await fs.unlink(path.join(directory, 'nodes', 'node.py'));
  await fs.symlink(path.join(root, 'outside.py'), path.join(directory, 'nodes', 'node.py'));
  assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: 'nodes/node.py' })).error.code, 'flowx/artifact-unavailable');
  map.nodes.First.artifacts.source = path.join(directory, 'nodes', 'escape.py');
  assert.equal((await call(service, 'inspect', { workflowId: 'wf1' })).error.code, 'flowx/artifact-unavailable');
});

test('graph JSON sanitizes decoded values without changing topology or JSON keys', async t => {
  const { env, workspace } = await fixture(t);
  env.EXTRA_ACCESS_TOKEN = 'short';
  const directory = path.join(workspace, 'workflows', 'wf1');
  await fs.mkdir(directory, { recursive: true });
  const graphPath = path.join(directory, 'workflow.json');
  const graph = {
    nodes: [
      { name: 'Collect', type: 'Collect', desc: 'A quoted token="unknown value" and test-secret-value', depends: [], ext_data: { type: 'url', desc: 'Bearer abc123', api_key: 'unknown-credential', nested: { password: 'with spaces' } } },
      { name: 'Publish', type: 'Publish', desc: 'Use https://alice:pw@example.test and short', depends: ['Collect'], inputs_format: { password: 'string', input: 'string' } },
    ],
    nested: ['safe', { note: 'authorization: "Bearer other-token"', secret: ['a', { text: 'hidden' }] }],
  };
  const encoded = JSON.stringify(graph).replace('test-secret-value', 'test-secret-\\u0076alue');
  await fs.writeFile(graphPath, encoded);
  const map = { workflow_id: 'wf1', workflow_version: 1, requirement: null, graph: graphPath, nodes: {}, main_entrypoint: { id: 'm1', version: 1, path: null } };
  const fake = fakeSpawn(() => ({ ok: true, operation: 'artifacts', data: map }));
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  await value(service, 'inspect', { workflowId: 'wf1' });
  const read = await value(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' });
  const safe = JSON.parse(read.content);
  assert.deepEqual(safe.nodes.map(node => [node.name, node.type, node.depends]), [['Collect', 'Collect', []], ['Publish', 'Publish', ['Collect']]]);
  assert.equal(safe.nodes[0].ext_data.api_key, '[REDACTED]');
  assert.equal(safe.nodes[0].ext_data.nested.password, '[REDACTED]');
  assert.equal(safe.nodes[1].inputs_format.password, 'string');
  assert.deepEqual(safe.nested[1].secret, ['[REDACTED]', { text: '[REDACTED]' }]);
  assert.doesNotMatch(read.content, /test-secret-value|unknown value|unknown-credential|with spaces|abc123|alice:pw|other-token|short/);
  assert.ok(read.content.includes('"nodes"'));

  // No secret-bearing identifiers, dependencies, types or property names are renamed.
  for (const unsafe of [
    { nodes: [{ name: 'test-secret-value', depends: [] }] },
    { nodes: [{ name: 'Collect', depends: ['test-secret-value'] }] },
    { nodes: [{ name: 'Collect', type: 'test-secret-value' }] },
    { nodes: [{ name: 'Collect', inputs_format: { 'test-secret-value': 'string' } }] },
    { nodes: [{ name: 'Collect', inputs_format: { password: 'Bearer sensitive' } }] },
    { nodes: [{ name: 'Collect', nodeName: 'test-secret-value' }] },
    { nodes: [{ name: 'Collect', id: 'password=unknown' }] },
  ]) {
    await fs.writeFile(graphPath, JSON.stringify(unsafe));
    const result = await call(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' });
    assert.equal(result.error.code, 'flowx/artifact-unavailable');
    assert.doesNotMatch(JSON.stringify(result), /test-secret-value|password=unknown/);
  }
  await fs.writeFile(graphPath, '{invalid');
  assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' })).error.code, 'flowx/artifact-unavailable');
  await fs.writeFile(graphPath, '{"nodes":[],"value":9007199254740993}');
  assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' })).error.code, 'flowx/artifact-unavailable');
});

test('cached graph snapshot is structurally sanitized and unsafe graphs are not cached', async t => {
  const { env, workspace } = await fixture(t);
  const directory = path.join(workspace, 'workflows', 'wf1');
  await fs.mkdir(directory, { recursive: true });
  const graph = path.join(directory, 'workflow.json');
  const map = { workflow_id: 'wf1', workflow_version: 1, requirement: null, graph, nodes: {}, main_entrypoint: { id: 'm1', version: 1, path: null } };
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  await fs.writeFile(graph, JSON.stringify({ nodes: [{ name: 'A', desc: 'token="unknown value"', depends: [] }] }));
  const first = await startFake(service, fake);
  first.emitJson({ event: 'result', result: { ok: true, status: 'completed', workflow_id: 'wf1', artifacts: map } });
  first.exit();
  await waitFor(() => service.state.status === 'completed' && !service.active);
  const next = await startFake(service, fake);
  const cached = await value(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' });
  assert.equal(cached.cached, true);
  assert.equal(JSON.parse(cached.content).nodes[0].desc, 'token=[REDACTED]');
  next.exit(1);
  await waitFor(() => !service.active);

  await fs.writeFile(graph, JSON.stringify({ nodes: [{ name: 'test-secret-value', depends: [] }] }));
  const third = await startFake(service, fake);
  third.emitJson({ event: 'result', result: { ok: true, status: 'completed', workflow_id: 'wf1', artifacts: map } });
  third.exit();
  await waitFor(() => service.state.status === 'completed' && !service.active);
  const fourth = await startFake(service, fake);
  assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' })).error.code, 'flowx/busy');
  fourth.exit(1);
  await waitFor(() => !service.active);
});

test('inspection times out, is bounded, and excludes concurrent builds', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn(() => null);
  const service = new FlowXHostService({ env, spawn: fake.spawn, inspectTimeoutMs: 40, shutdownMs: 20 });
  t.after(() => service.dispose());
  const pending = call(service, 'list');
  await waitFor(() => fake.calls.length > 0);
  assert.equal((await call(service, 'start', { prompt: 'x' })).error.code, 'flowx/busy');
  assert.equal((await pending).error.code, 'flowx/inspection-timeout');
  const huge = fakeSpawn(() => 'x'.repeat(LIMIT + 1));
  const another = new FlowXHostService({ env, spawn: huge.spawn });
  t.after(() => another.dispose());
  assert.equal((await call(another, 'list')).error.code, 'flowx/inspection-failed');
});

test('dispose waits for an inspection racing before child spawn', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  const pending = call(service, 'list');
  await service.dispose();
  assert.equal((await pending).error.code, 'flowx/disposed');
  assert.equal(fake.calls.length, 0);
  assert.equal(service.state.status, 'disposed');
});

test('real child receives a private prompt file and exchanges JSON lines over one persistent process', async t => {
  const { env, skill } = await fixture(t);
  const script = `
const fs = require('node:fs');
const readline = require('node:readline');
const args = process.argv.slice(2);
const file = args[args.indexOf('--prompt-file') + 1];
if (fs.readFileSync(file, 'utf8') !== 'private prompt' || (fs.statSync(file).mode & 0o077)) process.exit(3);
if (args[args.indexOf('--host') + 1] !== '127.0.0.1') process.exit(4);
const emit = value => process.stdout.write(JSON.stringify(value) + '\\n');
emit({event:'allocated',workflow_id:'wf1'});
emit({event:'started',workflow_id:'wf1'});
emit({event:'input_required',workflow_id:'wf1',request_id:'req1',awaiting:{stepId:'step1',prompt:'repair',test_log:'failed'}});
readline.createInterface({input:process.stdin}).on('line', line => {
  const input = JSON.parse(line);
  if (input.action !== 'answer' || input.input !== 'fix' || input.step_id !== 'step1') process.exit(5);
  emit({event:'result',result:{workflow_id:'wf1',ok:true,status:'completed',artifacts:null}});
  process.exit(0);
});`;
  await fs.writeFile(path.join(skill, 'scripts', 'build.py'), script);
  const service = new FlowXHostService({ env });
  t.after(() => service.dispose());
  await value(service, 'start', { prompt: 'private prompt' });
  await waitFor(() => service.state.status === 'input_required');
  const paused = service.snapshot();
  await value(service, 'answer', { requestId: paused.requestId, stepId: paused.awaiting.stepId, revision: paused.revision, input: 'fix' });
  await waitFor(() => service.state.status === 'completed' && !service.active);
});

test('a completed graph snapshot remains readable during the next build', async t => {
  const { env, workspace } = await fixture(t);
  const directory = path.join(workspace, 'workflows', 'wf1');
  await fs.mkdir(path.join(directory, 'plan'), { recursive: true });
  const graph = path.join(directory, 'plan', 'workflow.json');
  await fs.writeFile(graph, '{"nodes":[]}');
  const artifacts = { workflow_id: 'wf1', workflow_version: 1, requirement: null, graph, nodes: {}, main_entrypoint: { id: 'm1', version: 1, path: null } };
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  const first = await startFake(service, fake);
  first.emitJson({ event: 'result', result: { ok: true, status: 'completed', workflow_id: 'wf1', artifacts } });
  first.exit();
  await waitFor(() => service.state.status === 'completed' && !service.active);
  await fs.writeFile(graph, 'changed');
  const second = await startFake(service, fake);
  const cached = await value(service, 'readArtifact', { workflowId: 'wf1', path: 'plan/workflow.json' });
  assert.equal(cached.content, '{"nodes":[]}');
  assert.equal(cached.cached, true);
  assert.equal((await call(service, 'inspect', { workflowId: 'wf1' })).error.code, 'flowx/busy');
  second.exit(1);
  await waitFor(() => service.state.status === 'failed' && !service.active);
});

test('oversized, binary and redirected artifacts and storage are refused', async t => {
  const { env, workspace, root } = await fixture(t);
  const directory = path.join(workspace, 'workflows', 'wf1');
  await fs.mkdir(directory, { recursive: true });
  const graph = path.join(directory, 'workflow.json');
  await fs.writeFile(graph, 'x'.repeat(LIMIT + 1));
  const map = { workflow_id: 'wf1', workflow_version: 1, requirement: null, graph, nodes: {}, main_entrypoint: { id: 'm1', version: 1, path: null } };
  const fake = fakeSpawn(() => ({ ok: true, operation: 'artifacts', data: map }));
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  await value(service, 'inspect', { workflowId: 'wf1' });
  assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' })).error.code, 'flowx/artifact-unavailable');
  await fs.writeFile(graph, Buffer.from([0xff]));
  assert.equal((await call(service, 'readArtifact', { workflowId: 'wf1', path: 'workflow.json' })).error.code, 'flowx/artifact-unavailable');
  const binary = path.join(directory, 'image.png');
  await fs.writeFile(binary, 'not a supported artifact');
  map.graph = binary;
  assert.equal((await call(service, 'inspect', { workflowId: 'wf1' })).error.code, 'flowx/artifact-unavailable');
  await fs.symlink(root, path.join(workspace, 'workflows', 'redirected'));
  const count = fake.calls.length;
  assert.equal((await call(service, 'list')).error.code, 'flowx/configuration');
  assert.equal(fake.calls.length, count);
});

test('dispose waits for pending startup, cancels paused child and clears its prompt file', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const starting = new FlowXHostService({ env, spawn: fake.spawn });
  const pending = call(starting, 'start', { prompt: 'private' });
  await starting.dispose();
  assert.equal((await pending).error.code, 'flowx/disposed');
  assert.equal(starting.state.status, 'disposed');
  assert.equal(fake.calls.length, 0);

  const service = new FlowXHostService({ env, spawn: fake.spawn, shutdownMs: 20 });
  const child = await startFake(service, fake);
  const promptFile = fake.calls.at(-1).args.at(-1);
  assert.equal(await fs.readFile(promptFile, 'utf8'), 'make a workflow');
  assert.equal((await fs.stat(promptFile)).mode & 0o077, 0);
  pause(child);
  const disposal = service.dispose();
  assert.deepEqual(JSON.parse(child.writes[0]), { action: 'abort' });
  await disposal;
  await assert.rejects(fs.stat(promptFile), { code: 'ENOENT' });
  assert.equal(service.state.status, 'disposed');
  t.after(() => service.dispose());
});

test('abort cannot race a late success result into completed', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  const child = await startFake(service, fake);
  pause(child);
  const paused = service.snapshot();
  await value(service, 'abort', { requestId: paused.requestId, stepId: paused.awaiting.stepId, revision: paused.revision });
  child.emitJson({ event: 'result', result: { workflow_id: 'wf1', ok: true, status: 'completed' } });
  child.exit();
  await waitFor(() => service.state.status === 'failed' && !service.active);
});