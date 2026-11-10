// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: exercise the DeepSeek Harness host without external services.
// Modified 2026-11-09: cover host schema, secret handling and bounded results.
// Modified 2026-11-09: validate the shared Fetch route and its authorization boundary.
// Modified 2026-11-10: cover private persistent settings and operation coordination.
// Modified 2026-11-10: cover symlinked DSH_HOME resolution and retained confinement.
// Modified 2026-11-10: exercise safe, catalog-bound workflow folder browsing.

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
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
const value = async (...args) => {
  const result = await call(...args);
  assert.equal(result.ok, true, JSON.stringify(result));
  // Settings results are compared against the submitted shape; the key itself is never returned.
  if (['getSettings', 'saveSettings'].includes(args[1])) { const { apiKeySet, ...rest } = result.value; return { ...rest, apiKey: '' }; }
  return result.value;
};
const waitFor = async (predicate, message = 'condition') => {
  const until = Date.now() + 5000;
  while (Date.now() < until) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail(`Timed out waiting for ${message}`);
};

async function fixture(t) {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'flowx-host-test-')));
  const workspace = path.join(root, 'workspace');
  const skill = path.join(root, 'skill');
  await fs.mkdir(path.join(skill, 'scripts'), { recursive: true });
  await fs.mkdir(workspace);
  await fs.writeFile(path.join(skill, 'scripts', 'build.py'), '');
  await fs.writeFile(path.join(skill, 'scripts', 'inspect_workflow.py'), '');
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  return { root, workspace, skill, env: { ...process.env, DSH_HOME: path.join(root, 'dsh'), FLOWX_WORKSPACE: workspace, FLOWX_PYTHON: process.execPath, FLOWX_SKILL_DIR: skill, FLOWX_LLM_API_KEY: 'test-secret-value' } };
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
    if (args[0] === '-c') {
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

test('default skill resolution does not add a new child environment variable', async t => {
  const { env } = await fixture(t);
  delete env.FLOWX_SKILL_DIR;
  const fake = fakeSpawn();
  const service = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => service.dispose());
  const child = await startFake(service, fake);
  assert.equal(Object.hasOwn(fake.calls[0].options.env, 'FLOWX_SKILL_DIR'), false);
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

const settingsFor = env => ({ python: env.FLOWX_PYTHON, workspace: env.FLOWX_WORKSPACE, apiKey: '' });
const shown = (s, apiKeySet = true) => ({ python: s.python, workspace: s.workspace, apiKeySet });
const onDisk = s => ({ python: s.python, workspace: s.workspace });
const settingsFile = env => path.join(env.DSH_HOME, 'plugins', 'deepseek-harness-flowx', 'settings.json');

test('settings use environment fallbacks and persist privately across service restarts', async t => {
  const { root, workspace, skill, env } = await fixture(t);
  const fallback = new FlowXHostService({ env: { ...env, FLOWX_PYTHON: undefined, FLOWX_WORKSPACE: undefined } });
  assert.deepEqual(await value(fallback, 'getSettings'), { python: null, workspace: null, apiKey: '' });
  await fallback.dispose();
  await assert.rejects(fs.lstat(env.DSH_HOME), { code: 'ENOENT' });

  const alternate = path.join(root, 'alternate');
  const interpreter = path.join(root, 'venv-python');
  await fs.mkdir(alternate);
  await fs.symlink(process.execPath, interpreter);
  const settings = { python: interpreter, workspace: alternate, apiKey: '' };
  const first = new FlowXHostService({ env });
  assert.deepEqual(await value(first, 'getSettings'), settingsFor(env));
  assert.deepEqual(await value(first, 'saveSettings', settings), settings);
  await assert.rejects(fs.lstat(path.join(alternate, 'workflows')), { code: 'ENOENT' });
  const filename = settingsFile(env);
  const disk = await fs.readFile(filename, 'utf8');
  assert.deepEqual(JSON.parse(disk), onDisk(settings));
  assert.doesNotMatch(disk, /test-secret-value|FLOWX_LLM_API_KEY/);
  assert.equal((await fs.stat(filename)).mode & 0o777, 0o600);
  assert.equal((await fs.stat(path.dirname(filename))).mode & 0o777, 0o700);
  await first.dispose();

  const fake = fakeSpawn();
  const second = new FlowXHostService({ env: { ...env, FLOWX_WORKSPACE: workspace }, spawn: fake.spawn });
  t.after(() => second.dispose());
  assert.deepEqual(await value(second, 'getSettings'), settings);
  const child = await startFake(second, fake);
  assert.equal(fake.calls[0].command, interpreter);
  assert.equal(fake.calls[0].args[2], alternate);
  assert.equal(fake.calls[0].options.env.FLOWX_PYTHON, interpreter);
  assert.equal(fake.calls[0].options.env.FLOWX_WORKSPACE, alternate);
  assert.equal(Object.hasOwn(fake.calls[0].options.env, 'FLOWX_SKILL_DIR'), true);
  child.exit(1);
  await waitFor(() => !second.active);
});

test('settings reject unauthorized callers and malformed payloads without writing', async t => {
  const { env } = await fixture(t);
  const service = new FlowXHostService({ env });
  t.after(() => service.dispose());
  const settings = settingsFor(env);
  for (const peer of [undefined, {}, { principal: { kind: 'guest' } }]) {
    assert.equal((await service.dispatch('getSettings', null, peer)).error.code, 'flowx/forbidden');
    assert.equal((await service.dispatch('saveSettings', settings, peer)).error.code, 'flowx/forbidden');
  }
  for (const [endpoint, payload] of [
    ['getSettings', {}], ['getSettings', undefined], ['saveSettings', null], ['saveSettings', {}],
    ['saveSettings', { ...settings, secret: 'private' }], ['saveSettings', { ...settings, python: null }],
    ['saveSettings', { ...settings, workspace: 'x'.repeat(4097) }],
  ]) {
    assert.equal((await service.dispatch(endpoint, payload, OWNER)).error.code, 'flowx/invalid-payload');
  }
  await assert.rejects(fs.lstat(env.DSH_HOME), { code: 'ENOENT' });
});

test('settings endpoints are reachable through the existing owner-admitted route', async t => {
  const { env } = await fixture(t);
  const service = new FlowXHostService({ env });
  const dispatch = FlowXHostService.prototype.dispatch;
  t.mock.method(FlowXHostService.prototype, 'dispatch', function (endpoint, payload, peer) {
    return dispatch.call(service, endpoint, payload, peer);
  });
  const { route, cleanup } = mountRoute();
  t.after(async () => { await cleanup(); await service.dispose(); });
  const get = await route.fetch(jsonRequest({ endpoint: 'getSettings', payload: null }));
  assert.deepEqual((await get.json()).value, shown(settingsFor(env)));
  const save = await route.fetch(jsonRequest({ endpoint: 'saveSettings', payload: settingsFor(env) }));
  assert.deepEqual((await save.json()).value, shown(settingsFor(env)));
  assert.deepEqual(JSON.parse(await fs.readFile(settingsFile(env), 'utf8')), onDisk(settingsFor(env)));
});

test('invalid paths, unsafe scripts and storage cannot mutate saved settings or create workflows', async t => {
  const { root, workspace, skill, env } = await fixture(t);
  const service = new FlowXHostService({ env });
  t.after(() => service.dispose());
  const initial = settingsFor(env);
  await value(service, 'saveSettings', initial);
  const filename = settingsFile(env);
  const before = await fs.readFile(filename);
  const secondWorkspace = path.join(root, 'new-workspace');
  const nonExecutable = path.join(root, 'not-executable');
  const outside = path.join(root, 'outside.py');
  const badSkill = path.join(root, 'bad-skill');
  const redirectedWorkspace = path.join(root, 'redirected-workspace');
  const secretWorkspace = path.join(root, 'test-secret-value');
  await fs.mkdir(secondWorkspace);
  await fs.writeFile(nonExecutable, '', { mode: 0o600 });
  await fs.writeFile(outside, '');
  await fs.mkdir(path.join(badSkill, 'scripts'), { recursive: true });
  await fs.symlink(outside, path.join(badSkill, 'scripts', 'build.py'));
  await fs.writeFile(path.join(badSkill, 'scripts', 'inspect_workflow.py'), '');
  await fs.mkdir(redirectedWorkspace);
  await fs.mkdir(secretWorkspace);
  await fs.symlink(workspace, path.join(redirectedWorkspace, 'workflows'));
  for (const settings of [
    { ...initial, workspace: 'relative' }, { ...initial, python: '' },
    { ...initial, workspace: '/missing-test-secret-value' }, { ...initial, python: nonExecutable },
    { ...initial, workspace: redirectedWorkspace },
    { ...initial, workspace: secretWorkspace }, { ...initial, workspace: secondWorkspace, python: '/missing' },
    { ...initial, workspace: `${workspace}\0bad` },
  ]) {
    const result = await call(service, 'saveSettings', settings);
    assert.equal(result.error.code, 'flowx/configuration');
    assert.doesNotMatch(JSON.stringify(result), /test-secret-value|flowx-host-test-/);
    assert.deepEqual(await fs.readFile(filename), before);
    assert.deepEqual(await value(service, 'getSettings'), initial);
  }
  await assert.rejects(fs.lstat(path.join(workspace, 'workflows')), { code: 'ENOENT' });
  await assert.rejects(fs.lstat(path.join(secondWorkspace, 'workflows')), { code: 'ENOENT' });
  assert.deepEqual(await fs.readdir(path.dirname(filename)), ['settings.json']);
  assert.equal(skill, env.FLOWX_SKILL_DIR);
});

test('invalid persisted settings fail visibly and are never silently overwritten', async t => {
  const { root, env } = await fixture(t);
  const service = new FlowXHostService({ env });
  t.after(() => service.dispose());
  const settings = settingsFor(env);
  await value(service, 'saveSettings', settings);
  const filename = settingsFile(env);
  const good = JSON.stringify(onDisk(settings));
  for (const corrupt of ['{', 'null', JSON.stringify({ ...settings, token: 'secret' }), JSON.stringify({ ...settings, workspace: 'relative' }), 'x'.repeat(16385), Buffer.from([0xff])]) {
    await fs.writeFile(filename, corrupt);
    const before = await fs.readFile(filename);
    for (const [endpoint, payload] of [['getSettings', null], ['list', null], ['saveSettings', settings]]) {
      const result = await call(service, endpoint, payload);
      assert.equal(result.error.code, 'flowx/settings-invalid');
      assert.doesNotMatch(JSON.stringify(result), /flowx-host-test-|secret/);
    }
    assert.deepEqual(await fs.readFile(filename), before);
  }
  await fs.writeFile(filename, good);
  await fs.chmod(filename, 0o644);
  assert.equal((await call(service, 'getSettings')).error.code, 'flowx/settings-invalid');
  assert.equal((await call(service, 'saveSettings', settings)).error.code, 'flowx/settings-invalid');
  await fs.chmod(filename, 0o600);
  const hardlink = path.join(root, 'hardlink');
  await fs.link(filename, hardlink);
  assert.equal((await call(service, 'getSettings')).error.code, 'flowx/settings-invalid');
  await fs.unlink(hardlink);
  assert.deepEqual(await value(service, 'getSettings'), settings);
});

test('settings storage rejects redirected directories and files without touching targets', async t => {
  const { root, env } = await fixture(t);
  const outside = path.join(root, 'outside');
  await fs.mkdir(outside);
  const target = path.join(outside, 'target');
  await fs.writeFile(target, 'untouched', { mode: 0o600 });
  const settings = settingsFor(env);
  for (const position of ['plugins', 'plugin', 'file']) {
    const home = path.join(root, `home-${position}`);
    const plugins = path.join(home, 'plugins');
    const plugin = path.join(plugins, 'deepseek-harness-flowx');
    await fs.mkdir(home, { mode: 0o700 });
    if (position === 'plugins') await fs.symlink(outside, plugins);
    else {
      await fs.mkdir(plugins, { mode: 0o700 });
      if (position === 'plugin') await fs.symlink(outside, plugin);
      else {
        await fs.mkdir(plugin, { mode: 0o700 });
        await fs.symlink(target, path.join(plugin, 'settings.json'));
      }
    }
    const service = new FlowXHostService({ env: { ...env, DSH_HOME: home } });
    const expected = position === 'file' ? 'flowx/settings-invalid' : 'flowx/settings-storage';
    assert.equal((await call(service, 'getSettings')).error.code, expected, position);
    assert.equal((await call(service, 'saveSettings', settings)).error.code, expected, position);
    assert.equal(await fs.readFile(target, 'utf8'), 'untouched');
    assert.deepEqual(await fs.readdir(outside), ['target']);
    await service.dispose();
  }
  const insecure = path.join(root, 'insecure');
  await fs.mkdir(insecure, { mode: 0o777 });
  await fs.chmod(insecure, 0o777);
  const linked = path.join(root, 'linked-home');
  await fs.symlink(insecure, linked);
  const service = new FlowXHostService({ env: { ...env, DSH_HOME: linked } });
  assert.equal((await call(service, 'getSettings')).error.code, 'flowx/settings-storage');
  assert.equal((await call(service, 'saveSettings', settings)).error.code, 'flowx/settings-storage');
  assert.deepEqual(await fs.readdir(insecure), []);
  await service.dispose();
  for (const home of ['', 'relative-home', `${root}\0bad`, `${root}/home-home/../other`]) {
    const service = new FlowXHostService({ env: { ...env, DSH_HOME: home } });
    assert.equal((await call(service, 'getSettings')).error.code, 'flowx/settings-storage');
    assert.equal((await call(service, 'saveSettings', settings)).error.code, 'flowx/settings-storage');
    await service.dispose();
  }
});

test('settings storage resolves a symlinked DSH_HOME and still confines its own directory', async t => {
  const { root, env } = await fixture(t);
  const realHome = path.join(root, 'real-home');
  const linkedHome = path.join(root, 'linked-home');
  await fs.mkdir(realHome, { mode: 0o700 });
  await fs.chmod(realHome, 0o700);
  await fs.symlink(realHome, linkedHome);
  const service = new FlowXHostService({ env: { ...env, DSH_HOME: linkedHome } });
  assert.deepEqual(await value(service, 'getSettings'), settingsFor(env));
  await value(service, 'saveSettings', settingsFor(env));
  const canonical = path.join(realHome, 'plugins', 'deepseek-harness-flowx');
  assert.equal(await fs.realpath(path.join(linkedHome, 'plugins', 'deepseek-harness-flowx')), await fs.realpath(canonical));
  assert.equal((await fs.stat(path.join(canonical, 'settings.json'))).mode & 0o777, 0o600);
  await service.dispose();
  const reopened = new FlowXHostService({ env: { ...env, DSH_HOME: linkedHome } });
  t.after(() => reopened.dispose());
  assert.deepEqual(await value(reopened, 'getSettings'), settingsFor(env));
});

test('failed atomic saves preserve the previous file and remove temporary files', async t => {
  const { root, env } = await fixture(t);
  const service = new FlowXHostService({ env });
  t.after(() => service.dispose());
  const initial = settingsFor(env);
  await value(service, 'saveSettings', initial);
  const workspace = path.join(root, 'next');
  await fs.mkdir(workspace);
  const filename = settingsFile(env);
  const before = await fs.readFile(filename);
  const original = service.readSettingsFile.bind(service);
  let reads = 0;
  service.readSettingsFile = async directory => {
    if (++reads === 3) throw new Error('private path');
    return original(directory);
  };
  const result = await call(service, 'saveSettings', { ...initial, workspace });
  assert.equal(result.error.code, 'flowx/settings-storage');
  assert.doesNotMatch(JSON.stringify(result), /private path/);
  service.readSettingsFile = original;
  assert.deepEqual(await fs.readFile(filename), before);
  assert.deepEqual(await fs.readdir(path.dirname(filename)), ['settings.json']);
  assert.deepEqual(await value(service, 'getSettings'), initial);
});

test('settings writes serialize across instances and block operations without stale caches', async t => {
  const { root, env } = await fixture(t);
  const fake = fakeSpawn(() => ({ ok: true, operation: 'list', data: {} }));
  const first = new FlowXHostService({ env, spawn: fake.spawn });
  const second = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(async () => { await first.dispose(); await second.dispose(); });
  const a = path.join(root, 'a'), b = path.join(root, 'b');
  await fs.mkdir(a);
  await fs.mkdir(b);
  const initial = settingsFor(env);
  const settingsA = { ...initial, workspace: a }, settingsB = { ...initial, workspace: b };
  let resume;
  const gate = new Promise(resolve => { resume = resolve; });
  const validate = first.validateConfig.bind(first);
  let entered = false;
  first.validateConfig = async (...args) => { entered = true; await gate; return validate(...args); };
  first.artifacts.set('old', { root: env.FLOWX_WORKSPACE, map: {} });
  second.artifacts.set('old', { root: env.FLOWX_WORKSPACE, map: {} });
  first.graphSnapshot = { workflowId: 'old', path: 'graph.json', content: 'old' };
  second.graphSnapshot = { workflowId: 'old', path: 'graph.json', content: 'old' };
  const pendingA = call(first, 'saveSettings', settingsA);
  await waitFor(() => entered);
  const pendingB = call(second, 'saveSettings', settingsB);
  for (const [endpoint, payload] of [['getSettings', null], ['list', null], ['start', { prompt: 'x' }], ['readArtifact', { workflowId: 'old', path: 'graph.json' }]]) {
    assert.equal((await call(second, endpoint, payload)).error.code, 'flowx/busy');
  }
  // The queued request must own its input, even if its caller later mutates it.
  settingsB.workspace = '/changed-after-dispatch';
  resume();
  assert.deepEqual((await pendingA).value, shown(settingsA));
  assert.deepEqual((await pendingB).value, shown({ ...initial, workspace: b }));
  assert.deepEqual(await value(first, 'getSettings'), { ...initial, workspace: b });
  assert.equal(first.artifacts.size, 0);
  assert.equal(second.artifacts.size, 0);
  assert.equal(first.graphSnapshot, null);
  assert.equal(second.graphSnapshot, null);
  assert.equal((await call(first, 'readArtifact', { workflowId: 'old', path: 'graph.json' })).error.code, 'flowx/artifact-unavailable');
  assert.deepEqual(await value(second, 'list'), { workflows: {} });
  assert.equal(fake.calls.at(-1).args[2], b);
});

test('settings cannot race active inspections or builds and dispose waits for a pending save', async t => {
  const { env } = await fixture(t);
  const fake = fakeSpawn(() => null);
  const service = new FlowXHostService({ env, spawn: fake.spawn, shutdownMs: 10 });
  const other = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(async () => { await service.dispose(); await other.dispose(); });
  const settings = settingsFor(env);
  const inspection = call(service, 'list');
  await waitFor(() => fake.calls.length > 0);
  assert.equal((await call(other, 'saveSettings', settings)).error.code, 'flowx/busy');
  fake.calls.at(-1).child.stdout.write(JSON.stringify({ ok: true, operation: 'list', data: {} }));
  fake.calls.at(-1).child.exit();
  assert.equal((await inspection).ok, true);
  const child = await startFake(service, fake);
  assert.equal((await call(other, 'saveSettings', settings)).error.code, 'flowx/busy');
  assert.equal((await call(other, 'getSettings')).error.code, 'flowx/busy');
  child.exit(1);
  await waitFor(() => !service.active);
  await assert.rejects(fs.lstat(env.DSH_HOME), { code: 'ENOENT' });

  let resume;
  const gate = new Promise(resolve => { resume = resolve; });
  const validate = other.validateConfig.bind(other);
  let entered = false;
  other.validateConfig = async (...args) => { entered = true; await gate; return validate(...args); };
  const pending = call(other, 'saveSettings', settings);
  await waitFor(() => entered);
  const disposal = other.dispose();
  assert.equal((await call(service, 'list')).error.code, 'flowx/busy');
  resume();
  assert.equal((await pending).error.code, 'flowx/disposed');
  await disposal;
  await assert.rejects(fs.lstat(env.DSH_HOME), { code: 'ENOENT' });
});

test('default settings directory uses the actual home and rejects insecure directory modes', async t => {
  const { root, env } = await fixture(t);
  const defaultHome = path.join(root, 'default-home');
  await fs.mkdir(defaultHome, { mode: 0o700 });
  t.mock.method(os, 'homedir', () => defaultHome);
  const withoutDshHome = { ...env };
  delete withoutDshHome.DSH_HOME;
  const service = new FlowXHostService({ env: withoutDshHome });
  t.after(() => service.dispose());
  const settings = settingsFor(env);
  await value(service, 'saveSettings', settings);
  const filename = path.join(defaultHome, '.dsh', 'plugins', 'deepseek-harness-flowx', 'settings.json');
  assert.deepEqual(JSON.parse(await fs.readFile(filename, 'utf8')), onDisk(settings));
  await fs.chmod(path.join(defaultHome, '.dsh', 'plugins'), 0o777);
  assert.equal((await call(service, 'getSettings')).error.code, 'flowx/settings-storage');
  assert.equal((await call(service, 'saveSettings', settings)).error.code, 'flowx/settings-storage');
  await fs.chmod(path.join(defaultHome, '.dsh', 'plugins'), 0o700);
  await fs.chmod(path.dirname(filename), 0o755);
  assert.equal((await call(service, 'getSettings')).error.code, 'flowx/settings-storage');
  await fs.chmod(path.dirname(filename), 0o700);
  assert.deepEqual(await value(service, 'getSettings'), settings);
});

test('saved paths that become unavailable fail operations visibly without rewriting settings', async t => {
  const { root, env } = await fixture(t);
  const workspace = path.join(root, 'disappearing-workspace');
  await fs.mkdir(workspace);
  const settings = { ...settingsFor(env), workspace };
  const service = new FlowXHostService({ env });
  t.after(() => service.dispose());
  await value(service, 'saveSettings', settings);
  const original = await fs.readFile(settingsFile(env));
  await fs.rmdir(workspace);
  assert.deepEqual(await value(service, 'getSettings'), settings);
  for (const [endpoint, payload] of [['list', null], ['start', { prompt: 'x' }]]) {
    const result = await call(service, endpoint, payload);
    assert.equal(result.error.code, 'flowx/configuration');
    assert.doesNotMatch(JSON.stringify(result), /disappearing-workspace|flowx-host-test-/);
  }
  assert.deepEqual(await fs.readFile(settingsFile(env)), original);
  // A structurally valid but unavailable old path does not prevent explicit recovery.
  assert.deepEqual(await value(service, 'saveSettings', settingsFor(env)), settingsFor(env));
});

test('queued saves recover after a failure and settings reads exclude writes', async t => {
  const { env } = await fixture(t);
  const first = new FlowXHostService({ env });
  const second = new FlowXHostService({ env });
  t.after(async () => { await first.dispose(); await second.dispose(); });
  const settings = settingsFor(env);
  const bad = call(first, 'saveSettings', { ...settings, python: 'relative' });
  const good = call(second, 'saveSettings', settings);
  assert.equal((await bad).error.code, 'flowx/configuration');
  assert.deepEqual((await good).value, shown(settings));
  let resume;
  const gate = new Promise(resolve => { resume = resolve; });
  const load = first.loadSettings.bind(first);
  let entered = false;
  first.loadSettings = async () => { entered = true; await gate; return load(); };
  const pending = call(first, 'getSettings');
  await waitFor(() => entered);
  assert.equal((await call(second, 'saveSettings', settings)).error.code, 'flowx/busy');
  assert.equal((await call(second, 'start', { prompt: 'x' })).error.code, 'flowx/busy');
  resume();
  assert.deepEqual((await pending).value, shown(settings));
});

test('atomic save refuses a target swapped to a symlink after validation', async t => {
  const { root, env } = await fixture(t);
  const service = new FlowXHostService({ env });
  t.after(() => service.dispose());
  const settings = settingsFor(env);
  await value(service, 'saveSettings', settings);
  const filename = settingsFile(env);
  const outside = path.join(root, 'sensitive');
  await fs.writeFile(outside, 'untouched', { mode: 0o600 });
  const original = service.readSettingsFile.bind(service);
  let reads = 0;
  service.readSettingsFile = async directory => {
    if (++reads === 3) {
      await fs.unlink(filename);
      await fs.symlink(outside, filename);
    }
    return original(directory);
  };
  assert.equal((await call(service, 'saveSettings', settings)).error.code, 'flowx/settings-invalid');
  assert.equal(await fs.readFile(outside, 'utf8'), 'untouched');
  assert.equal((await fs.lstat(filename)).isSymbolicLink(), true);
  assert.deepEqual(await fs.readdir(path.dirname(filename)), ['settings.json']);
});

const folderCatalogEntry = () => ({ nodes: {}, workflow_version: 1, main_entrypoint_id: 'main1', errors: {}, has_graph: true, has_main: false });
const folderPayload = (path = '', workflowId = 'wf1') => ({ workflowId, path });

async function folderFixture(t, catalog = { wf1: folderCatalogEntry() }) {
  const result = await fixture(t);
  const directory = path.join(result.workspace, 'workflows', 'wf1');
  await fs.mkdir(directory, { recursive: true });
  await fs.writeFile(path.join(directory, 'agent.json'), JSON.stringify({ workflow_id: 'wf1', main_entrypoint_id: 'main1', errors: {} }));
  const fake = fakeSpawn(args => args[3] === 'list' ? { ok: true, operation: 'list', data: catalog } : { ok: false });
  const service = new FlowXHostService({ env: result.env, spawn: fake.spawn });
  t.after(() => service.dispose());
  return { ...result, directory, fake, service };
}

test('folder browsing lists the actual workflow tree and securely reads unlisted source and JSON', async t => {
  const { directory, root, service } = await folderFixture(t);
  await fs.mkdir(path.join(directory, 'plan'));
  await fs.mkdir(path.join(directory, 'src'));
  await fs.mkdir(path.join(directory, '.internal'));
  await fs.mkdir(path.join(directory, 'credentials'));
  await fs.writeFile(path.join(directory, 'src', 'main.py'), 'print("hello")\n# test-secret-value token=unknown');
  await fs.writeFile(path.join(directory, 'plan', 'workflow.json'), '{"nodes":[{"name":"A","depends":[],"api\\u005fkey":"unknown-key","password":123,"desc":"test-secret-\\u0076alue"}]}');
  await fs.writeFile(path.join(directory, 'manifest.json'), '{"secret":{"nested":"hidden"},"safe":"yes"}');
  await fs.writeFile(path.join(directory, 'agent.json'), JSON.stringify({ workflow_id: 'wf1', main_entrypoint_id: 'main1', main_entrypoint_path: '/private/source.py', errors: { graph: 'unlabeled sensitive log' } }));
  await fs.writeFile(path.join(directory, 'image.png'), 'binary image');
  await fs.writeFile(path.join(directory, 'binary.txt'), Buffer.from([0x61, 0, 0x62]));
  await fs.writeFile(path.join(directory, 'bad-utf8.py'), Buffer.from([0xff]));
  await fs.writeFile(path.join(directory, 'invalid.json'), '{not json');
  await fs.writeFile(path.join(directory, '.env'), 'private');
  await fs.writeFile(path.join(directory, 'secret.txt'), 'private');
  await fs.writeFile(path.join(directory, 'test-secret-value.txt'), 'private');
  await fs.writeFile(path.join(directory, 'private.pem'), 'private');
  await fs.writeFile(path.join(directory, 'bad\\name.py'), 'private');
  await fs.writeFile(path.join(root, 'outside.txt'), 'outside private');
  await fs.symlink(path.join(root, 'outside.txt'), path.join(directory, 'link.txt'));
  await fs.symlink(path.join(directory, 'src'), path.join(directory, 'linkdir'));
  await fs.link(path.join(root, 'outside.txt'), path.join(directory, 'hardlink.txt'));
  if (process.platform !== 'win32') {
    assert.equal(spawnSync('mkfifo', [path.join(directory, 'pipe.txt')]).status, 0);
  }

  const listing = await value(service, 'listFolder', folderPayload());
  assert.equal(listing.workflowId, 'wf1');
  assert.equal(listing.path, '');
  assert.equal(listing.truncated, false);
  const byName = Object.fromEntries(listing.entries.map(entry => [entry.name, entry]));
  assert.deepEqual(byName.src, { name: 'src', path: 'src', type: 'directory', readable: true });
  assert.deepEqual(byName['agent.json'], { name: 'agent.json', path: 'agent.json', type: 'file', readable: true });
  for (const name of ['image.png', 'binary.txt', 'bad-utf8.py', 'invalid.json', 'hardlink.txt']) assert.equal(byName[name].readable, false, name);
  for (const name of ['.env', '.internal', 'credentials', 'secret.txt', 'test-secret-value.txt', 'private.pem', 'bad\\name.py', 'link.txt', 'linkdir', 'pipe.txt']) assert.equal(Object.hasOwn(byName, name), false, name);
  assert.doesNotMatch(JSON.stringify(listing), /test-secret-value|outside private/);
  assert.deepEqual((await value(service, 'listFolder', folderPayload('src'))).entries, [{ name: 'main.py', path: 'src/main.py', type: 'file', readable: true }]);
  const source = await value(service, 'readFolderFile', folderPayload('src/main.py'));
  assert.equal(source.path, 'src/main.py');
  assert.equal(source.cached, false);
  assert.doesNotMatch(source.content, /test-secret-value|token=unknown/);
  const graph = JSON.parse((await value(service, 'readFolderFile', folderPayload('plan/workflow.json'))).content);
  assert.deepEqual(graph.nodes[0], { name: 'A', depends: [], api_key: '[REDACTED]', password: '[REDACTED]', desc: '[REDACTED]' });
  const manifest = JSON.parse((await value(service, 'readFolderFile', folderPayload('manifest.json'))).content);
  assert.deepEqual(manifest, { secret: { nested: '[REDACTED]' }, safe: 'yes' });
  const agent = JSON.parse((await value(service, 'readFolderFile', folderPayload('agent.json'))).content);
  assert.equal(agent.errors, 'Details withheld.');
  assert.equal(agent.main_entrypoint_path, '[withheld]');
  for (const filename of ['image.png', 'binary.txt', 'bad-utf8.py', 'invalid.json', 'hardlink.txt', 'link.txt', 'linkdir/main.py', '.env', '.internal/x.py', 'credentials/x.py', 'secret.txt', 'private.pem', 'pipe.txt']) {
    assert.equal((await call(service, 'readFolderFile', folderPayload(filename))).error.code, 'flowx/artifact-unavailable', filename);
  }
  for (const folder of ['linkdir', 'linkdir/sub', '.internal', 'credentials', 'src/main.py']) {
    assert.equal((await call(service, 'listFolder', folderPayload(folder))).error.code, 'flowx/artifact-unavailable', folder);
  }
});

test('folder reads apply graph sanitization to mapped text aliases and reject unsafe graph identity', async t => {
  const { directory, workspace, service } = await folderFixture(t);
  const filename = path.join(directory, 'graph.txt');
  await fs.writeFile(filename, '{"nodes":[{"name":"A","depends":[],"api\\u005fkey":"unknown"}]}');
  service.artifacts.set('wf1', { root: workspace, map: { graph: 'graph.txt' } });
  const result = await value(service, 'readFolderFile', folderPayload('graph.txt'));
  assert.equal(JSON.parse(result.content).nodes[0].api_key, '[REDACTED]');
  await fs.writeFile(filename, '{"nodes":[{"name":"test-secret-\\u0076alue","depends":[]}]}');
  assert.equal((await call(service, 'readFolderFile', folderPayload('graph.txt'))).error.code, 'flowx/artifact-unavailable');
  assert.equal((await value(service, 'listFolder', folderPayload())).entries.find(entry => entry.name === 'graph.txt').readable, false);
  await fs.writeFile(filename, 'not JSON');
  assert.equal((await call(service, 'readFolderFile', folderPayload('graph.txt'))).error.code, 'flowx/artifact-unavailable');
});

test('folder paths are strict, owner-only, and require a matching regular catalog manifest', async t => {
  const { directory, root, workspace, service } = await folderFixture(t);
  await fs.writeFile(path.join(directory, 'plain.py'), 'safe');
  for (const peer of [undefined, {}, { principal: { kind: 'guest' } }, { kind: 'owner' }]) {
    for (const endpoint of ['listFolder', 'readFolderFile']) assert.equal((await service.dispatch(endpoint, folderPayload('plain.py'), peer)).error.code, 'flowx/forbidden');
  }
  for (const endpoint of ['listFolder', 'readFolderFile']) {
    for (const candidate of ['../plain.py', 'src/../plain.py', '/etc/passwd', 'src//file.py', 'src/./file.py', 'src\\file.py', 'x\0y', 'x'.repeat(4097)]) {
      assert.equal((await call(service, endpoint, folderPayload(candidate))).error.code, 'flowx/invalid-payload', candidate);
    }
    for (const payload of [null, {}, { workflowId: '../wf1', path: '' }, { ...folderPayload(), extra: true }, { workflowId: 'wf1', path: null }]) {
      assert.equal((await service.dispatch(endpoint, payload, OWNER)).error.code, 'flowx/invalid-payload');
    }
  }
  assert.equal((await call(service, 'readFolderFile', folderPayload())).error.code, 'flowx/invalid-payload');
  assert.equal((await call(service, 'listFolder', folderPayload('control\nname'))).error.code, 'flowx/artifact-unavailable');
  assert.equal((await call(service, 'listFolder', folderPayload('', 'missing'))).error.code, 'flowx/artifact-unavailable');
  const noMarker = path.join(workspace, 'workflows', 'missing');
  await fs.mkdir(noMarker);
  await fs.writeFile(path.join(noMarker, 'plain.py'), 'private');
  assert.equal((await call(service, 'readFolderFile', folderPayload('plain.py', 'missing'))).error.code, 'flowx/artifact-unavailable');

  const marker = path.join(directory, 'agent.json');
  for (const content of ['{}', '{', JSON.stringify({ workflow_id: 'other' }), JSON.stringify({ workflow_id: 'wf1', password: 'bad' }) + '\0']) {
    await fs.writeFile(marker, content);
    assert.equal((await call(service, 'listFolder', folderPayload())).error.code, 'flowx/artifact-unavailable');
  }
  await fs.unlink(marker);
  await fs.writeFile(path.join(root, 'outside-marker.json'), JSON.stringify({ workflow_id: 'wf1' }));
  await fs.symlink(path.join(root, 'outside-marker.json'), marker);
  assert.equal((await call(service, 'listFolder', folderPayload())).ok, false);
  await fs.unlink(marker);
  await fs.mkdir(marker);
  assert.equal((await call(service, 'listFolder', folderPayload())).error.code, 'flowx/artifact-unavailable');
  await fs.rmdir(marker);
  await fs.writeFile(marker, JSON.stringify({ workflow_id: 'wf1' }));
  service.catalog = async () => ({ workflows: {} });
  assert.equal((await call(service, 'listFolder', folderPayload())).error.code, 'flowx/artifact-unavailable');
  assert.equal((await call(service, 'readFolderFile', folderPayload('plain.py'))).error.code, 'flowx/artifact-unavailable');
});

test('folder browsing rejects redirected workflow roots and nested symlink paths', async t => {
  const { directory, root, workspace, service } = await folderFixture(t);
  const outside = path.join(root, 'outside');
  await fs.mkdir(outside);
  await fs.writeFile(path.join(outside, 'data.py'), 'sensitive');
  await fs.mkdir(path.join(directory, 'inner'));
  await fs.symlink(outside, path.join(directory, 'inner', 'escape'));
  await fs.symlink(path.join(outside, 'data.py'), path.join(directory, 'escape.py'));
  assert.deepEqual((await value(service, 'listFolder', folderPayload('inner'))).entries, []);
  for (const [endpoint, candidate] of [['listFolder', 'inner/escape'], ['readFolderFile', 'inner/escape/data.py'], ['readFolderFile', 'escape.py']]) {
    assert.equal((await call(service, endpoint, folderPayload(candidate))).error.code, 'flowx/artifact-unavailable');
  }
  const workflows = path.join(workspace, 'workflows');
  await fs.rename(directory, path.join(workflows, 'actual'));
  await fs.symlink(path.join(workflows, 'actual'), directory);
  assert.equal((await call(service, 'listFolder', folderPayload())).error.code, 'flowx/configuration');
  assert.equal((await call(service, 'readFolderFile', folderPayload('escape.py'))).error.code, 'flowx/configuration');
});

test('folder listings and UTF-8 reads enforce byte, entry, scan and probe limits', async t => {
  const { directory, service } = await folderFixture(t);
  await fs.mkdir(path.join(directory, 'many'));
  await Promise.all(Array.from({ length: 205 }, (_, i) => fs.writeFile(path.join(directory, 'many', `file-${String(i).padStart(3, '0')}.py`), 'x')));
  const many = await value(service, 'listFolder', folderPayload('many'));
  assert.equal(many.entries.length, 200);
  assert.equal(many.truncated, true);
  assert.deepEqual(many.entries.map(entry => entry.name), many.entries.map(entry => entry.name).sort());
  await fs.mkdir(path.join(directory, 'hidden'));
  await Promise.all(Array.from({ length: 1001 }, (_, i) => fs.writeFile(path.join(directory, 'hidden', `.hidden-${i}`), 'x')));
  const hidden = await value(service, 'listFolder', folderPayload('hidden'));
  assert.deepEqual(hidden.entries, []);
  assert.equal(hidden.truncated, true);

  await fs.mkdir(path.join(directory, 'large'));
  for (let i = 0; i < 5; i++) await fs.writeFile(path.join(directory, 'large', `${i}.txt`), 'é'.repeat(LIMIT / 2));
  await fs.writeFile(path.join(directory, 'large', 'oversized.txt'), 'x'.repeat(LIMIT + 1));
  const large = await value(service, 'listFolder', folderPayload('large'));
  assert.equal(large.truncated, true);
  assert.equal(large.entries.filter(entry => entry.readable).length, 4);
  assert.equal(large.entries.find(entry => entry.name === 'oversized.txt').readable, false);
  const exact = await value(service, 'readFolderFile', folderPayload('large/0.txt'));
  assert.equal(exact.bytes, LIMIT);
  assert.equal(Buffer.byteLength(exact.content), LIMIT);
  assert.equal((await call(service, 'readFolderFile', folderPayload('large/oversized.txt'))).error.code, 'flowx/artifact-unavailable');
  const longName = 'a'.repeat(100);
  let longRelative = '';
  // Darwin's small PATH_MAX prevents reaching the metadata cap with 200 real
  // entries; Linux can exercise the cap using a deeper directory.
  for (let i = 0; i < (process.platform === 'darwin' ? 6 : 12); i++) {
    longRelative = longRelative ? `${longRelative}/${longName}` : longName;
    await fs.mkdir(path.join(directory, ...longRelative.split('/')));
  }
  await Promise.all(Array.from({ length: 200 }, (_, i) => fs.writeFile(path.join(directory, ...longRelative.split('/'), `item-${String(i).padStart(3, '0')}-${'b'.repeat(100)}.py`), 'x')));
  const metadataLimited = await value(service, 'listFolder', folderPayload(longRelative));
  if (process.platform !== 'darwin') {
    assert.equal(metadataLimited.truncated, true);
    assert.ok(metadataLimited.entries.length < 200);
  } else assert.equal(metadataLimited.entries.length, 200);
  assert.ok(Buffer.byteLength(JSON.stringify(metadataLimited)) < 256 * 1024 + 4096);
});

test('folder reads and enumeration fail closed if a file or directory is replaced', async t => {
  const { directory, root, service } = await folderFixture(t);
  const outside = path.join(root, 'outside');
  await fs.mkdir(outside);
  await fs.writeFile(path.join(outside, 'data.py'), 'outside secret');
  const folder = path.join(directory, 'source');
  await fs.mkdir(folder);
  await fs.writeFile(path.join(folder, 'data.py'), 'safe');
  const readMapped = service.readMapped.bind(service);
  service.readMapped = async (...args) => {
    if (args[2] === 'source/data.py') {
      await fs.unlink(path.join(folder, 'data.py'));
      await fs.symlink(path.join(outside, 'data.py'), path.join(folder, 'data.py'));
    }
    return readMapped(...args);
  };
  assert.equal((await call(service, 'readFolderFile', folderPayload('source/data.py'))).error.code, 'flowx/artifact-unavailable');
  service.readMapped = readMapped;
  const folderDirectory = service.folderDirectory.bind(service);
  let checks = 0;
  service.folderDirectory = async (...args) => {
    if (++checks === 2) {
      await fs.rename(folder, path.join(directory, 'original'));
      await fs.symlink(outside, folder);
    }
    return folderDirectory(...args);
  };
  const listing = await call(service, 'listFolder', folderPayload('source'));
  assert.equal(listing.error.code, 'flowx/artifact-unavailable');
  assert.doesNotMatch(JSON.stringify(listing), /outside secret/);
});

test('folder operations coordinate with builds, settings writes, and disposal', async t => {
  const { root, directory, env, service, fake } = await folderFixture(t);
  const other = new FlowXHostService({ env, spawn: fake.spawn });
  t.after(() => other.dispose());
  await fs.writeFile(path.join(directory, 'main.py'), 'safe');
  let resume;
  let entered = false;
  const gate = new Promise(resolve => { resume = resolve; });
  const catalog = service.catalog.bind(service);
  service.catalog = async (...args) => { entered = true; await gate; return catalog(...args); };
  const pending = call(service, 'listFolder', folderPayload());
  await waitFor(() => entered);
  assert.equal((await call(other, 'start', { prompt: 'x' })).error.code, 'flowx/busy');
  assert.equal((await call(other, 'saveSettings', settingsFor(env))).error.code, 'flowx/busy');
  resume();
  assert.equal((await pending).ok, true);
  service.catalog = catalog;

  let resumeRead;
  let reading = false;
  const readGate = new Promise(resolve => { resumeRead = resolve; });
  service.catalog = async (...args) => { reading = true; await readGate; return catalog(...args); };
  const pendingRead = call(service, 'readFolderFile', folderPayload('main.py'));
  await waitFor(() => reading);
  assert.equal((await call(other, 'start', { prompt: 'x' })).error.code, 'flowx/busy');
  assert.equal((await call(other, 'saveSettings', settingsFor(env))).error.code, 'flowx/busy');
  resumeRead();
  assert.equal((await pendingRead).ok, true);
  service.catalog = catalog;

  const child = await startFake(other, fake);
  for (const [endpoint, payload] of [['listFolder', folderPayload()], ['readFolderFile', folderPayload('main.py')]]) {
    assert.equal((await call(service, endpoint, payload)).error.code, 'flowx/busy');
  }
  child.exit(1);
  await waitFor(() => !other.active);

  let resumeSave;
  let saving = false;
  const saveGate = new Promise(resolve => { resumeSave = resolve; });
  const validate = other.validateConfig.bind(other);
  other.validateConfig = async (...args) => { saving = true; await saveGate; return validate(...args); };
  const alternate = path.join(root, 'alternate');
  await fs.mkdir(alternate);
  const save = call(other, 'saveSettings', { ...settingsFor(env), workspace: alternate });
  await waitFor(() => saving);
  for (const [endpoint, payload] of [['listFolder', folderPayload()], ['readFolderFile', folderPayload('main.py')]]) {
    assert.equal((await call(service, endpoint, payload)).error.code, 'flowx/busy');
  }
  resumeSave();
  assert.equal((await save).ok, true);
  assert.equal((await call(service, 'listFolder', folderPayload())).error.code, 'flowx/artifact-unavailable');
  assert.equal((await call(service, 'readFolderFile', folderPayload('main.py'))).error.code, 'flowx/artifact-unavailable');

  let resumeDispose;
  let inspecting = false;
  const disposeGate = new Promise(resolve => { resumeDispose = resolve; });
  const config = service.config.bind(service);
  service.config = async () => { inspecting = true; await disposeGate; return config(); };
  const browsing = call(service, 'listFolder', folderPayload());
  await waitFor(() => inspecting);
  const disposal = service.dispose();
  resumeDispose();
  assert.equal((await browsing).error.code, 'flowx/disposed');
  await disposal;
});