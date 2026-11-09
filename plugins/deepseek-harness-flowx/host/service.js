// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: owner-only, process-local workflow build and inspection host.
// Modified 2026-11-09: bound and sanitize host responses and limit inherited credentials.

import { spawn as nodeSpawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import * as fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const LIMIT = 1024 * 1024;
const BUILD_EVENT_LIMIT = 16 * LIMIT;
const REPAIR_TEXT_LIMIT = 8192;
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const TEXT_TYPES = new Set(['.json', '.md', '.py', '.txt', '.log']);
const DEFAULT_SKILL = fileURLToPath(new URL('../../../skills/flowx-workflows/', import.meta.url));
const PROVIDER_CREDENTIALS = new Set(['FLOWX_LLM_API_KEY', 'OPENAI_API_KEY', 'ZHIPU_API_KEY', 'DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'ONEONEONEAPI_API_KEY']);
const CREDENTIAL_PATHS = new Set(['GOOGLE_APPLICATION_CREDENTIALS', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE', 'AZURE_CONFIG_DIR']);
const PROVIDER_URLS = new Set(['FLOWX_LLM_BASE_URL', 'OPENAI_BASE_URL', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']);
const SECRET_ENV = /(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE|COOKIE|JWT|(?:^|_)PAT(?:_|$))/i;
const SECRET_FIELD = /(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|authorization|private[_-]?key)/i;
const GRAPH_ID_FIELD = /^(?:id|ids|name|type|depends|dependenc(?:y|ies)|requires|source|target|from|to|node|step|parent|ref|reference|.*[_-](?:ids?|names?|refs?)|.*(?:Ids?|Names?|Refs?))$/i;
const URL_CREDENTIALS = /\b[a-z][a-z0-9+.-]*:\/\/[^\s/@:]+:[^\s/@]+@/i;
const STAGES = ['reformat', 'graph', 'nodes', 'main'];
const STAGE_STATUSES = new Set(['completed', 'skipped', 'failed', 'blocked', 'awaiting']);
// This only coordinates service instances loaded from this module in this Node process.
let buildOwner = null;
let inspections = 0;

class HostError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const problem = (code, message) => new HostError(`flowx/${code}`, message);
const invalid = () => problem('invalid-payload', 'Invalid endpoint payload.');
const busy = () => problem('busy', 'A build or inspection is active in this plugin process.');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(k => Object.hasOwn(value, k));
const text = (value, max = LIMIT) => typeof value === 'string' && Buffer.byteLength(value) <= max;
const identifier = value => text(value, 512) && value.trim().length > 0 && !/[\x00-\x1f\x7f]/.test(value);
const workflowId = value => typeof value === 'string' && ID.test(value);
const inside = (root, candidate) => candidate !== root && !path.relative(root, candidate).startsWith(`..${path.sep}`) && path.relative(root, candidate) !== '..' && !path.isAbsolute(path.relative(root, candidate));
const relativeArtifact = value => text(value, 4096) && value.length > 0 && !value.includes('\\') && !value.includes('\0') && !path.posix.isAbsolute(value) && value.split('/').every(part => part && part !== '.' && part !== '..');

function safeError(error) {
  return error instanceof HostError
    ? { code: error.code, message: error.message, details: {} }
    : { code: 'flowx/internal', message: 'The operation failed.', details: {} };
}

/**
 * Transport contract: dispatch(endpoint, payload, peer) returns Connection RPC
 * {ok:true,value} or {ok:false,error:{code,message,details:{}}}. status/list take null;
 * start takes {prompt}; answer takes {requestId,stepId,revision,input}; abort
 * takes {requestId,stepId,revision}; inspect takes {workflowId}; readArtifact
 * takes {workflowId,path}, where path is relative to that workflow's directory.
 * The owner must treat prompts, repair context, artifacts and logs as sensitive.
 * Nothing here locks other FlowX processes or prevents external workspace edits.
 * Revisions are scoped to instanceId; callers should compare both across reloads.
 * Child processes are not sandboxed: they retain the host user's filesystem,
 * network and other OS privileges, including access to HOME and credential files.
 * Environment filtering reduces accidental inheritance, not those privileges.
 */
export class FlowXHostService {
  constructor({ env = process.env, spawn = nodeSpawn, inspectTimeoutMs = 30000, abortTimeoutMs = 30000, shutdownMs = 2000 } = {}) {
    this.env = { ...env };
    const secrets = new Set();
    for (const [key, value] of Object.entries(this.env)) {
      if (typeof value !== 'string' || !value) continue;
      if (SECRET_ENV.test(key)) secrets.add(value);
      if (URL_CREDENTIALS.test(value)) {
        try {
          const url = new URL(value);
          for (const part of [url.username, url.password]) if (part) { secrets.add(part); secrets.add(decodeURIComponent(part)); }
        } catch { /* malformed URLs are not a source of separately known secrets */ }
      }
    }
    const ordered = [...secrets].sort((a, b) => b.length - a.length);
    this.secretPattern = ordered.length ? new RegExp(ordered.map(secret => secret.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g') : null;
    this.spawn = spawn;
    this.inspectTimeoutMs = inspectTimeoutMs;
    this.abortTimeoutMs = abortTimeoutMs;
    this.shutdownMs = shutdownMs;
    this.state = { instanceId: randomUUID(), revision: 0, status: 'idle', workflowId: null, requestId: null, awaiting: null, result: null, error: null };
    this.active = null;
    this.children = new Set();
    this.inspectionOps = new Set();
    this.artifacts = new Map();
    this.graphSnapshot = null;
    this.closed = false;
    this.disposePromise = null;
  }

  snapshot() { return structuredClone(this.state); }
  update(changes) { this.state = { ...this.state, ...changes, revision: this.state.revision + 1 }; }

  async dispatch(endpoint, payload, peer) {
    if (peer?.principal?.kind !== 'owner') return { ok: false, error: safeError(problem('forbidden', 'Only the owner may use FlowX.')) };
    try {
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      let value;
      switch (endpoint) {
        case 'status': if (payload !== null) throw invalid(); value = this.snapshot(); break;
        case 'start':
          if (!exact(payload, ['prompt']) || !text(payload.prompt) || !payload.prompt.trim()) throw invalid();
          value = await this.start(payload.prompt); break;
        case 'answer':
          if (!exact(payload, ['requestId', 'stepId', 'revision', 'input']) || !identifier(payload.requestId) || !identifier(payload.stepId) || !Number.isSafeInteger(payload.revision) || !text(payload.input)) throw invalid();
          if (Buffer.byteLength(JSON.stringify({ action: 'answer', step_id: payload.stepId, input: payload.input })) + 1 > LIMIT) throw invalid();
          value = this.respond(payload, false); break;
        case 'abort':
          if (!exact(payload, ['requestId', 'stepId', 'revision']) || !identifier(payload.requestId) || !identifier(payload.stepId) || !Number.isSafeInteger(payload.revision)) throw invalid();
          value = this.respond(payload, true); break;
        case 'list': if (payload !== null) throw invalid(); value = await this.list(); break;
        case 'inspect':
          if (!exact(payload, ['workflowId']) || !workflowId(payload.workflowId)) throw invalid();
          value = await this.inspect(payload.workflowId); break;
        case 'readArtifact':
          if (!exact(payload, ['workflowId', 'path']) || !workflowId(payload.workflowId) || !relativeArtifact(payload.path)) throw invalid();
          value = await this.readArtifact(payload.workflowId, payload.path); break;
        default: throw problem('unknown-endpoint', 'Unknown endpoint.');
      }
      return { ok: true, value };
    } catch (error) { return { ok: false, error: safeError(error) }; }
  }

  async config() {
    const workspace = this.env.FLOWX_WORKSPACE;
    const python = this.env.FLOWX_PYTHON;
    const skill = this.env.FLOWX_SKILL_DIR ?? DEFAULT_SKILL;
    if (![workspace, python, skill].every(p => typeof p === 'string' && path.isAbsolute(p))) {
      throw problem('configuration', 'FLOWX_WORKSPACE, FLOWX_PYTHON and FLOWX_SKILL_DIR must be absolute paths.');
    }
    try {
      // Execute the configured Python path, not its realpath: a venv's symlink
      // must remain argv[0] so Python discovers pyvenv.cfg and its packages.
      const [root, skillRoot] = await Promise.all([fs.realpath(workspace), fs.realpath(skill)]);
      const interpreter = path.normalize(python);
      const [rootStat, pythonStat, skillStat] = await Promise.all([fs.stat(root), fs.stat(interpreter), fs.stat(skillRoot)]);
      if (!rootStat.isDirectory() || !pythonStat.isFile() || !skillStat.isDirectory()) throw new Error();
      await fs.access(interpreter, constants.X_OK);
      const scripts = {};
      for (const name of ['build.py', 'inspect_workflow.py']) {
        const script = await fs.realpath(path.join(skillRoot, 'scripts', name));
        if (!inside(skillRoot, script) || !(await fs.stat(script)).isFile()) throw new Error();
        scripts[name] = script;
      }
      await this.checkStorage(root);
      return { root, interpreter, scripts };
    } catch { throw problem('configuration', 'FlowX paths must refer to an existing workspace, executable interpreter and skill scripts.'); }
  }

  async checkStorage(root) {
    // The SDK scans */agent.json even for list and before building a new workflow.
    // Refuse redirects there before handing the workspace to Python.
    const directory = path.join(root, 'workflows');
    await fs.mkdir(directory, { recursive: false }).catch(error => { if (error.code !== 'EEXIST') throw error; });
    if (await fs.realpath(directory) !== directory || !(await fs.stat(directory)).isDirectory()) throw new Error();
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) throw new Error();
      if (!entry.isDirectory()) continue;
      try {
        if ((await fs.lstat(path.join(directory, entry.name, 'agent.json'))).isSymbolicLink()) throw new Error();
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }

  spawnChild(command, args) {
    // Keep Python, proxy and provider configuration, but drop unrelated secrets.
    // Credential paths and the explicitly supported FlowX/LLM keys are retained.
    const env = Object.fromEntries(Object.entries(this.env).filter(([key, value]) => {
      const upper = key.toUpperCase();
      if (PROVIDER_CREDENTIALS.has(upper) || CREDENTIAL_PATHS.has(upper) || PROVIDER_URLS.has(upper)) return true;
      return !SECRET_ENV.test(key) && !(typeof value === 'string' && URL_CREDENTIALS.test(value));
    }));
    const child = this.spawn(command, args, { env, stdio: ['pipe', 'pipe', 'pipe'], shell: false, detached: process.platform !== 'win32' });
    this.children.add(child);
    // stderr can contain credentials and arbitrary test output: drain, never retain or log it.
    child.stderr?.resume();
    child.stdin?.on('error', () => {});
    child.on('close', () => this.children.delete(child));
    return child;
  }

  signal(child, signal) {
    if (!this.children.has(child)) return;
    try {
      if (process.platform !== 'win32' && child.pid) process.kill(-child.pid, signal);
      else child.kill(signal);
    } catch { try { child.kill(signal); } catch { /* already exited */ } }
  }

  stopChild(child, graceful = false) {
    if (!this.children.has(child)) return;
    const timer = setTimeout(() => this.signal(child, 'SIGKILL'), this.shutdownMs);
    child.once('close', () => clearTimeout(timer));
    if (graceful && child.stdin?.writable) {
      try { child.stdin.end('{"action":"abort"}\n'); } catch { /* already closed */ }
    } else this.signal(child, 'SIGTERM');
    if (graceful) {
      const term = setTimeout(() => this.signal(child, 'SIGTERM'), Math.max(1, Math.floor(this.shutdownMs / 2)));
      child.once('close', () => clearTimeout(term));
    }
  }

  async start(prompt) {
    if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
    if (buildOwner || inspections) throw busy();
    buildOwner = this;
    const run = { child: null, temp: null, terminal: null, failure: null, abortRequested: false, done: null };
    run.startFinished = new Promise(resolve => { run.finishStart = resolve; });
    this.active = run;
    this.update({ status: 'starting', workflowId: null, requestId: null, awaiting: null, result: null, error: null });
    try {
      const config = await this.config();
      run.temp = await fs.mkdtemp(path.join(os.tmpdir(), 'flowx-harness-'));
      const promptFile = path.join(run.temp, 'prompt.txt');
      await fs.writeFile(promptFile, prompt, { mode: 0o600, flag: 'wx' });
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      run.config = config;
      const child = this.spawnChild(config.interpreter, [config.scripts['build.py'], '--workspace', config.root, 'new', '--host', '127.0.0.1', '--prompt-file', promptFile]);
      run.child = child;
      run.done = this.watchBuild(run);
      this.update({ status: 'running' });
      return this.snapshot();
    } catch (error) {
      await this.cleanupRun(run);
      this.update({ status: 'failed', error: { code: 'flowx/start-failed', message: 'Could not start the build.' } });
      throw error instanceof HostError ? error : problem('start-failed', 'Could not start the build.');
    } finally { run.finishStart(); }
  }

  watchBuild(run) {
    const child = run.child;
    let chunks = [], size = 0;
    child.stdout.on('data', chunk => {
      if (run.failure) return;
      const data = Buffer.from(chunk);
      let start = 0;
      while (start < data.length) {
        const newline = data.indexOf(10, start);
        const end = newline < 0 ? data.length : newline;
        const part = data.subarray(start, end);
        if (size + part.length > BUILD_EVENT_LIMIT) {
          chunks = []; size = 0;
          this.failBuild(run, 'The build emitted an oversized event; its outcome is unknown. A candidate workflow may exist; inspect it before retrying.', true);
          return;
        }
        chunks.push(part); size += part.length;
        if (newline < 0) return;
        const line = Buffer.concat(chunks, size);
        chunks = []; size = 0;
        start = newline + 1;
        try {
          const event = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(line));
          this.buildEvent(run, event);
        } catch { this.failBuild(run, 'The build emitted an invalid event.'); return; }
        if (run.failure) return;
      }
    });
    child.stdout.on('error', () => this.failBuild(run, 'The build output channel failed.'));
    child.on('error', () => { run.failure ||= 'The build process could not be started.'; });
    return new Promise(resolve => child.once('close', async (code, signal) => {
      run.exited = true;
      try {
        if (size && !run.failure) run.failure = 'The build emitted an incomplete event.';
        const result = run.terminal;
        if (run.failure) {
          this.update({ status: run.uncertain ? 'unknown' : 'failed', requestId: null, awaiting: null, error: { code: run.uncertain ? 'flowx/build-uncertain' : 'flowx/build-failed', message: run.failure } });
        } else if (result?.kind === 'aborted' && run.abortRequested) {
          this.update({ status: 'aborted', requestId: null, awaiting: null, result: null, error: null });
        } else if (!run.abortRequested && result?.kind === 'result' && code === 0 && !signal && result.ok === true && result.status === 'completed') {
          // A cached graph is a point-in-time copy from a successful result, never a live read during another build.
          this.graphSnapshot = null;
          this.artifacts.delete(result.workflowId);
          try {
            if (Buffer.byteLength(JSON.stringify(result.artifacts)) > LIMIT) throw new Error();
            const map = await this.normalizeArtifacts(run.config.root, result.workflowId, result.artifacts);
            this.artifacts.set(result.workflowId, { root: run.config.root, map });
            if (map.graph) this.graphSnapshot = await this.readMapped(run.config.root, result.workflowId, map.graph, true);
          } catch { /* completion does not depend on graph caching */ }
          this.update({ status: 'completed', workflowId: result.workflowId, requestId: null, awaiting: null, result: result.summary, error: null });
        } else {
          this.update({ status: 'failed', requestId: null, awaiting: null, result: result?.kind === 'result' ? result.summary : null, error: { code: 'flowx/build-failed', message: 'The build did not complete successfully.' } });
        }
      } finally { await this.cleanupRun(run); resolve(); }
    }));
  }

  failBuild(run, message, uncertain = false) {
    if (run.failure || run.exited || this.active !== run) return;
    run.failure = message;
    run.uncertain = uncertain;
    this.update({ status: 'stopping', requestId: null, awaiting: null });
    this.stopChild(run.child);
  }

  redact(value) {
    // A single replacement pass avoids reprocessing markers and exponential growth.
    const result = this.secretPattern ? value.replace(this.secretPattern, () => '[REDACTED]') : value;
    return result.replace(/\bBearer\s+[^\s"']+/gi, 'Bearer [REDACTED]')
      .replace(/(\b(?:api[_-]?key|(?:access|refresh)[_-]?token|token|secret|password|passwd|credential|authorization)["']?\s*[=:]\s*)(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s"',;]+)/gi, '$1[REDACTED]')
      .replace(/(\b[a-z][a-z0-9+.-]*:\/\/)[^\s/@:]+:[^\s/@]+@/gi, '$1[REDACTED]@');
  }

  safeIdentifier(value) {
    return identifier(value) && this.redact(value) === value;
  }

  sanitizeGraph(content) {
    const graph = JSON.parse(content);
    const visit = (value, depth = 0, identity = false, credential = false, inputSchema = false) => {
      if (depth > 100) throw new Error();
      if (typeof value === 'string') {
        const redacted = credential ? '[REDACTED]' : this.redact(value);
        // Graph names, types and references must never be silently changed.
        if (identity && redacted !== value) throw new Error();
        return redacted;
      }
      if (Array.isArray(value)) return value.map(item => visit(item, depth + 1, identity, credential));
      if (object(value)) return Object.fromEntries(Object.entries(value).map(([key, item]) => {
        if (this.redact(key) !== key) throw new Error();
        // inputs_format contains declared types, not credentials. Its values
        // must be known schema types; reject ambiguous values rather than leak.
        if (inputSchema && (typeof item !== 'string' || !/^(?:string|number|integer|boolean|object|array|null)$/i.test(item))) throw new Error();
        return [key, visit(item, depth + 1, identity || GRAPH_ID_FIELD.test(key) || inputSchema,
          credential || (SECRET_FIELD.test(key) && !inputSchema), key === 'inputs_format')];
      }));
      // Secret fields can also contain numeric or boolean credentials.
      if (credential && value !== null) return '[REDACTED]';
      if (typeof value === 'number' && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) throw new Error();
      return value;
    };
    return JSON.stringify(visit(graph));
  }

  summarizeResult(result) {
    const summary = { ok: result.ok, status: result.status, workflowId: result.workflow_id };
    if (object(result.stages)) {
      summary.stages = {};
      for (const name of STAGES) {
        const stage = result.stages[name];
        if (!object(stage) || !STAGE_STATUSES.has(stage.status)) continue;
        summary.stages[name] = { status: stage.status, hasError: !!stage.error };
        if (Number.isSafeInteger(stage.attempts) && stage.attempts >= 0) summary.stages[name].attempts = Math.min(stage.attempts, 999999);
      }
    }
    if (object(result.errors)) {
      const counts = { reformat: 0, graph: 0, nodes: 0, main: 0, other: 0 };
      for (const key of Object.keys(result.errors)) {
        const category = STAGES.find(name => key === name || key.startsWith(`${name}/`)) ?? 'other';
        counts[category] = Math.min(counts[category] + 1, 999999);
      }
      // Errors, stage error strings and logs may contain arbitrary credentials.
      summary.errors = { counts, details: 'Withheld.' };
    }
    return summary;
  }

  buildEvent(run, event) {
    if (!object(event) || typeof event.event !== 'string' || run.terminal) throw new Error();
    if (event.event === 'allocated' || event.event === 'started') {
      if (!workflowId(event.workflow_id) || !this.safeIdentifier(event.workflow_id) || (this.state.workflowId && this.state.workflowId !== event.workflow_id)) throw new Error();
      this.update({ workflowId: event.workflow_id });
    } else if (event.event === 'input_required') {
      if (this.state.status !== 'running' || !workflowId(event.workflow_id) || event.workflow_id !== this.state.workflowId || !this.safeIdentifier(event.request_id) || !object(event.awaiting) || !this.safeIdentifier(event.awaiting.stepId)) throw new Error();
      const awaiting = { stepId: event.awaiting.stepId, prompt: null, test_log: null };
      for (const key of ['prompt', 'test_log']) {
        if (event.awaiting[key] != null) {
          if (!text(event.awaiting[key], BUILD_EVENT_LIMIT)) throw new Error();
          const safe = Buffer.from(this.redact(event.awaiting[key]));
          awaiting[key] = safe.length > REPAIR_TEXT_LIMIT ? `${safe.subarray(0, REPAIR_TEXT_LIMIT).toString('utf8')}\n[truncated]` : safe.toString('utf8');
        }
      }
      this.update({ status: 'input_required', workflowId: event.workflow_id, requestId: event.request_id, awaiting });
    } else if (event.event === 'result') {
      const result = event.result;
      if (!object(result) || !workflowId(result.workflow_id) || result.workflow_id !== this.state.workflowId || typeof result.ok !== 'boolean' || !['completed', 'input_required'].includes(result.status)) throw new Error();
      run.terminal = { kind: 'result', ok: result.ok, status: result.status, workflowId: result.workflow_id, artifacts: result.artifacts, summary: this.summarizeResult(result) };
      this.update({ status: 'finishing', requestId: null, awaiting: null });
    } else if (event.event === 'aborted') {
      if (!run.abortRequested || (event.workflow_id != null && event.workflow_id !== this.state.workflowId)) throw new Error();
      run.terminal = { kind: 'aborted' };
      this.update({ status: 'finishing', requestId: null, awaiting: null });
    } else if (event.event === 'error') {
      // Python exception text may contain secrets, paths and prompt fragments.
      this.failBuild(run, 'The build reported an error.');
    } else throw new Error();
  }

  respond(payload, abort) {
    const run = this.active;
    if (!run?.child || this.state.status !== 'input_required' || payload.requestId !== this.state.requestId || payload.stepId !== this.state.awaiting?.stepId || payload.revision !== this.state.revision) {
      throw problem('stale-input', 'The repair request is no longer current.');
    }
    if (!run.child.stdin?.writable) throw problem('build-failed', 'The build input channel is closed.');
    run.abortRequested = abort;
    this.update({ status: abort ? 'aborting' : 'running', requestId: null, awaiting: null });
    try {
      run.child.stdin.write(JSON.stringify(abort ? { action: 'abort' } : { action: 'answer', step_id: payload.stepId, input: payload.input }) + '\n', error => {
        if (error) this.failBuild(run, 'The build input channel failed.');
      });
      if (abort) {
        const timer = setTimeout(() => { this.failBuild(run, 'The build did not stop after abort.'); }, this.abortTimeoutMs);
        timer.unref?.();
        run.child.once('close', () => clearTimeout(timer));
      }
    } catch { this.failBuild(run, 'The build input channel failed.'); throw problem('build-failed', 'The build input channel failed.'); }
    return this.snapshot();
  }

  async cleanupRun(run) {
    if (run.temp) await fs.rm(run.temp, { recursive: true, force: true }).catch(() => {});
    if (this.active === run) this.active = null;
    if (buildOwner === this) buildOwner = null;
  }

  async withInspection(operation) {
    if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
    if (buildOwner) throw busy();
    inspections++;
    let finish;
    const completion = new Promise(resolve => { finish = resolve; });
    this.inspectionOps.add(completion);
    try {
      const config = await this.config();
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      const result = await operation(config);
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      return result;
    } finally { inspections--; this.inspectionOps.delete(completion); finish(); }
  }

  async runInspection(config, operation, id) {
    if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
    const args = [config.scripts['inspect_workflow.py'], '--workspace', config.root, operation];
    if (id) args.push('--workflow-id', id);
    const child = this.spawnChild(config.interpreter, args);
    child.stdin.end();
    return new Promise((resolve, reject) => {
      let chunks = [], size = 0, failure = null;
      const timer = setTimeout(() => { failure = problem('inspection-timeout', 'Workflow inspection timed out.'); this.stopChild(child); }, this.inspectTimeoutMs);
      child.stdout.on('data', chunk => {
        size += chunk.length;
        if (size > LIMIT && !failure) { failure = problem('inspection-failed', 'Workflow inspection output exceeded the limit.'); chunks = []; this.stopChild(child); }
        else if (!failure) chunks.push(chunk);
      });
      child.stdout.on('error', () => { failure ||= problem('inspection-failed', 'Workflow inspection output failed.'); this.stopChild(child); });
      child.on('error', () => { failure ||= problem('inspection-failed', 'Workflow inspection could not be started.'); });
      child.once('close', code => {
        clearTimeout(timer);
        if (failure) return reject(failure);
        try {
          const data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
          if (code !== 0 || !object(data) || data.ok !== true || data.operation !== operation || !Object.hasOwn(data, 'data')) throw new Error();
          resolve(data.data);
        } catch { reject(problem('inspection-failed', 'Workflow inspection failed.')); }
      });
    });
  }

  async list() {
    return this.withInspection(async config => {
      const data = await this.runInspection(config, 'list');
      if (!object(data)) throw problem('inspection-failed', 'Workflow inspection returned invalid data.');
      const workflows = {};
      for (const [id, entry] of Object.entries(data)) {
        if (!workflowId(id) || !this.safeIdentifier(id) || !object(entry) || !object(entry.nodes) || !Number.isSafeInteger(entry.workflow_version) || entry.workflow_version < 0 || !this.safeIdentifier(entry.main_entrypoint_id) || typeof entry.has_graph !== 'boolean' || typeof entry.has_main !== 'boolean' || !object(entry.errors)) throw problem('inspection-failed', 'Workflow inspection returned invalid data.');
        const nodes = {};
        for (const [node, nodeId] of Object.entries(entry.nodes)) {
          if (!this.safeIdentifier(node) || (nodeId !== null && !this.safeIdentifier(nodeId))) throw problem('inspection-failed', 'Workflow inspection returned invalid data.');
          Object.defineProperty(nodes, node, { value: nodeId, enumerable: true });
        }
        // Catalog errors can contain arbitrary logs, paths and credentials; reveal only their redacted keys.
        const errors = {};
        for (const key of Object.keys(entry.errors)) Object.defineProperty(errors, this.redact(key), { value: 'Details withheld.', enumerable: true, configurable: true });
        workflows[id] = { nodes, workflow_version: entry.workflow_version, main_entrypoint_id: entry.main_entrypoint_id, has_graph: entry.has_graph, has_main: entry.has_main, errors };
      }
      return { workflows };
    });
  }

  async workflowRoot(root, id) {
    try {
      const directory = path.join(root, 'workflows');
      const workflows = await fs.realpath(directory);
      const target = await fs.realpath(path.join(directory, id));
      // Reject redirected workflow roots even when they point to another in-workspace workflow.
      if (workflows !== directory || target !== path.join(workflows, id) || !inside(root, workflows) || !inside(workflows, target) || !(await fs.stat(target)).isDirectory()) throw new Error();
      return target;
    } catch { throw problem('artifact-unavailable', 'The workflow directory is unavailable or unsafe.'); }
  }

  async normalizeArtifacts(root, id, data) {
    if (!object(data) || data.workflow_id !== id || !this.safeIdentifier(id) || !Number.isSafeInteger(data.workflow_version) || data.workflow_version < 0 || !object(data.nodes) || !object(data.main_entrypoint) || !this.safeIdentifier(data.main_entrypoint.id) || !Number.isSafeInteger(data.main_entrypoint.version) || data.main_entrypoint.version < 0) throw problem('inspection-failed', 'Workflow artifact data is invalid.');
    const directory = await this.workflowRoot(root, id);
    const normalize = async value => {
      if (value === null) return null;
      if (typeof value !== 'string' || !path.isAbsolute(value)) throw new Error();
      const resolved = await fs.realpath(value);
      const relative = path.relative(directory, resolved).split(path.sep).join('/');
      if (!inside(directory, resolved) || !relativeArtifact(relative) || this.redact(relative) !== relative || !TEXT_TYPES.has(path.extname(relative).toLowerCase()) || !(await fs.stat(resolved)).isFile()) throw new Error();
      return relative;
    };
    try {
      // Preserve the SDK's nested nodes -> artifacts map, including every artifact key.
      const result = { workflow_id: id, workflow_version: data.workflow_version, requirement: await normalize(data.requirement), graph: await normalize(data.graph), nodes: {}, main_entrypoint: { id: data.main_entrypoint.id, version: data.main_entrypoint.version, path: await normalize(data.main_entrypoint.path) } };
      for (const [name, node] of Object.entries(data.nodes)) {
        if (!this.safeIdentifier(name) || !object(node) || !this.safeIdentifier(node.node_id) || !Number.isSafeInteger(node.version) || node.version < 0 || !object(node.artifacts)) throw new Error();
        const artifacts = {};
        for (const [key, value] of Object.entries(node.artifacts)) {
          if (!this.safeIdentifier(key)) throw new Error();
          Object.defineProperty(artifacts, key, { value: await normalize(value), enumerable: true });
        }
        Object.defineProperty(result.nodes, name, { value: { node_id: node.node_id, version: node.version, artifacts }, enumerable: true });
      }
      return result;
    } catch { throw problem('artifact-unavailable', 'A workflow artifact is unavailable or unsafe.'); }
  }

  pathsIn(map) {
    const paths = [map.graph, map.requirement, map.main_entrypoint.path];
    for (const node of Object.values(map.nodes)) paths.push(...Object.values(node.artifacts));
    return new Set(paths.filter(value => typeof value === 'string'));
  }

  async inspect(id) {
    return this.withInspection(async config => {
      await this.workflowRoot(config.root, id);
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      const data = await this.runInspection(config, 'artifacts', id);
      const artifacts = await this.normalizeArtifacts(config.root, id, data);
      this.artifacts.set(id, { root: config.root, map: artifacts });
      return { artifacts: structuredClone(artifacts) };
    });
  }

  async readMapped(root, id, relative, graph = false) {
    if (!relativeArtifact(relative) || !TEXT_TYPES.has(path.extname(relative).toLowerCase())) throw problem('artifact-unavailable', 'The artifact is unavailable or unsafe.');
    let file;
    try {
      const directory = await this.workflowRoot(root, id);
      const candidate = path.join(directory, ...relative.split('/'));
      const real = await fs.realpath(candidate);
      if (!inside(directory, real) || real !== candidate) throw new Error();
      const before = await fs.stat(real);
      if (!before.isFile() || before.size > LIMIT) throw new Error();
      file = await fs.open(real, constants.O_RDONLY | (constants.O_NOFOLLOW || 0) | (constants.O_NONBLOCK || 0));
      const opened = await file.stat();
      if (!opened.isFile() || opened.size > LIMIT || opened.dev !== before.dev || opened.ino !== before.ino || await fs.realpath(candidate) !== real) throw new Error();
      const buffer = Buffer.alloc(LIMIT + 1);
      let offset = 0;
      while (offset < buffer.length) {
        const { bytesRead } = await file.read(buffer, offset, buffer.length - offset, offset);
        if (!bytesRead) break;
        offset += bytesRead;
      }
      if (offset > LIMIT) throw new Error();
      const source = new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, offset));
      const content = graph ? this.sanitizeGraph(source) : this.redact(source);
      if (!text(content)) throw new Error();
      return { workflowId: id, path: relative, content, bytes: offset, cached: false };
    } catch { throw problem('artifact-unavailable', 'The artifact is unavailable, unsafe, too large or not UTF-8 text.'); }
    finally { await file?.close().catch(() => {}); }
  }

  async readArtifact(id, relative) {
    if (buildOwner) {
      if (this.graphSnapshot?.workflowId === id && this.graphSnapshot.path === relative) return { ...this.graphSnapshot, cached: true };
      throw busy();
    }
    return this.withInspection(async config => {
      const entry = this.artifacts.get(id);
      if (!entry || entry.root !== config.root || !this.pathsIn(entry.map).has(relative)) throw problem('artifact-unavailable', 'Inspect the workflow before reading one of its listed artifacts.');
      return this.readMapped(config.root, id, relative, relative === entry.map.graph);
    });
  }

  dispose() {
    if (this.disposePromise) return this.disposePromise;
    this.closed = true;
    this.disposePromise = (async () => {
      const run = this.active;
      for (const child of this.children) this.stopChild(child, child === this.active?.child && this.state.status === 'input_required');
      if (run?.startFinished) await run.startFinished;
      if (run?.done) await run.done;
      await Promise.all([...this.children].map(child => new Promise(resolve => child.once('close', resolve))));
      await Promise.all([...this.inspectionOps]);
      this.artifacts.clear();
      this.graphSnapshot = null;
      this.update({ status: 'disposed', requestId: null, awaiting: null, result: null, error: null });
    })();
    return this.disposePromise;
  }
}