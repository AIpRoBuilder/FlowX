// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: owner-only, process-local workflow build and inspection host.
// Modified 2026-11-09: bound and sanitize host responses and limit inherited credentials.
// Modified 2026-11-10: persist owner-only path settings with private, serialized storage.
// Modified 2026-11-10: resolve a symlinked DSH_HOME before confining settings storage.
// Modified 2026-11-10: add bounded, owner-only workflow folder browsing.
// Modified 2026-11-10: save the API key in settings and locate the skill automatically.

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
const FOLDER_ENTRY_LIMIT = 200;
const FOLDER_SCAN_LIMIT = 1000;
const FOLDER_RESPONSE_LIMIT = 256 * 1024;
const FOLDER_PROBE_LIMIT = 4 * LIMIT;
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const TEXT_TYPES = new Set(['.json', '.md', '.py', '.txt', '.log']);
const DEFAULT_SKILL = fileURLToPath(new URL('../../../skills/flowx-workflows/', import.meta.url));
// Read-only inspection runs through the installed SDK; argv: workspace, operation[, workflow id].
const INSPECT_CODE = `import contextlib, json, sys
from flowx_sdk import FlowXClient
workspace, operation, *rest = sys.argv[1:]
try:
    with contextlib.redirect_stdout(sys.stderr), FlowXClient(workspace=workspace) as client:
        if operation == 'list': data = client.workflows()
        elif operation == 'nodes': data = client.list_nodes(rest[0])
        elif operation == 'artifacts': data = client.artifacts(rest[0])
        else: data = client.build_recovery_state(rest[0])
    print(json.dumps({'ok': True, 'operation': operation, 'data': data}, ensure_ascii=False, default=str))
except Exception as exc:
    print(json.dumps({'ok': False, 'error': type(exc).__name__}))
    sys.exit(1)
`;
const SETTINGS_KEYS = ['python', 'workspace'];
const SAVE_KEYS = ['python', 'workspace', 'apiKey'];
const SETTINGS_LIMIT = 16384;
const PROVIDER_CREDENTIALS = new Set(['FLOWX_LLM_API_KEY', 'OPENAI_API_KEY', 'ZHIPU_API_KEY', 'DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'ONEONEONEAPI_API_KEY']);
const CREDENTIAL_PATHS = new Set(['GOOGLE_APPLICATION_CREDENTIALS', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE', 'AZURE_CONFIG_DIR']);
const PROVIDER_URLS = new Set(['FLOWX_LLM_BASE_URL', 'OPENAI_BASE_URL', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY']);
const SECRET_ENV = /(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE|COOKIE|JWT|(?:^|_)PAT(?:_|$))/i;
const SECRET_FIELD = /(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|authorization|private[_-]?key)/i;
const SECRET_PATH = /(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential|authorization|private[_-]?key|(?:^|[._-])id[_-](?:rsa|dsa|ecdsa|ed25519)(?:[._-]|$)|\.(?:key|pem|p12|pfx)$)/i;
const GRAPH_ID_FIELD = /^(?:id|ids|name|type|depends|dependenc(?:y|ies)|requires|source|target|from|to|node|step|parent|ref|reference|.*[_-](?:ids?|names?|refs?)|.*(?:Ids?|Names?|Refs?))$/i;
const URL_CREDENTIALS = /\b[a-z][a-z0-9+.-]*:\/\/[^\s/@:]+:[^\s/@]+@/i;
const STAGES = ['reformat', 'graph', 'nodes', 'main'];
const STAGE_STATUSES = new Set(['completed', 'skipped', 'failed', 'blocked', 'awaiting']);
// This only coordinates service instances loaded from this module in this Node process.
let buildOwner = null;
let inspections = 0;
let settingsWrites = 0;
let settingsTail = Promise.resolve();
const services = new Set();
const serviceFinalizer = new FinalizationRegistry(reference => services.delete(reference));

class HostError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const problem = (code, message) => new HostError(`flowx/${code}`, message);
const invalid = () => problem('invalid-payload', 'Invalid endpoint payload.');
const busy = () => problem('busy', 'A build, inspection or settings change is active in this plugin process.');
const settingsError = () => problem('settings-invalid', 'The saved FlowX settings are invalid or unsafe.');
const storageError = () => problem('settings-storage', 'The FlowX settings storage is unavailable or unsafe.');
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
 * listFolder takes {workflowId,path} ('' means root) and returns {workflowId,
 * path,entries:[{name,path,type,readable}],truncated}. readFolderFile takes
 * {workflowId,path} and returns the same bounded text shape as readArtifact.
 * Folder listings omit hidden and credential-named paths, symlinks and special
 * files; readable files have passed the text/size checks at listing time.
 * The listing is capped at 200 entries, 1000 scanned entries, 256 KiB of entry
 * metadata and 4 MiB of text probes; truncated also signals a probe budget cap.
 * Folder reads are limited to 1 MiB, and JSON is structurally sanitized.
 * Manifest error details and absolute entrypoint paths are withheld. Redaction
 * is best-effort, and other workflow contents may still contain sensitive data.
 * getSettings takes null; saveSettings takes {python,workspace,apiKey} (empty
 * apiKey keeps the saved one) and both return {python,workspace,apiKeySet}
 * (unset environment fallbacks are null); the key is never returned.
 * The skill directory is not configurable: it is located automatically.
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
    this.secrets = secrets;
    this.secretPattern = this.buildSecretPattern();
    this.spawn = spawn;
    this.inspectTimeoutMs = inspectTimeoutMs;
    this.abortTimeoutMs = abortTimeoutMs;
    this.shutdownMs = shutdownMs;
    this.state = { instanceId: randomUUID(), revision: 0, status: 'idle', workflowId: null, requestId: null, awaiting: null, result: null, error: null };
    this.active = null;
    this.children = new Set();
    this.inspectionOps = new Set();
    this.settingsOps = new Set();
    this.artifacts = new Map();
    this.graphSnapshot = null;
    this.closed = false;
    this.disposePromise = null;
    this.serviceReference = new WeakRef(this);
    services.add(this.serviceReference);
    serviceFinalizer.register(this, this.serviceReference, this.serviceReference);
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
        case 'getSettings': if (payload !== null) throw invalid(); value = await this.getSettings(); break;
        case 'saveSettings':
          if (!exact(payload, SAVE_KEYS) || !SAVE_KEYS.every(key => text(payload[key], 4096))) throw invalid();
          value = await this.saveSettings({ python: payload.python, workspace: payload.workspace, apiKey: payload.apiKey }); break;
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
        case 'listFolder':
          if (!exact(payload, ['workflowId', 'path']) || !workflowId(payload.workflowId) || !(payload.path === '' || relativeArtifact(payload.path))) throw invalid();
          value = await this.listFolder(payload.workflowId, payload.path); break;
        case 'readFolderFile':
          if (!exact(payload, ['workflowId', 'path']) || !workflowId(payload.workflowId) || !relativeArtifact(payload.path)) throw invalid();
          value = await this.readFolderFile(payload.workflowId, payload.path); break;
        default: throw problem('unknown-endpoint', 'Unknown endpoint.');
      }
      return { ok: true, value };
    } catch (error) { return { ok: false, error: safeError(error) }; }
  }

  fallbackSettings() {
    return { python: this.env.FLOWX_PYTHON ?? null, workspace: this.env.FLOWX_WORKSPACE ?? null, apiKeySet: Boolean(this.env.FLOWX_LLM_API_KEY) };
  }

  buildSecretPattern() {
    const ordered = [...this.secrets].sort((a, b) => b.length - a.length);
    return ordered.length ? new RegExp(ordered.map(secret => secret.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g') : null;
  }

  // Resolution order: FLOWX_SKILL_DIR, the source checkout, then Harness skill roots.
  async skillRoot() {
    const home = this.env.DSH_HOME !== undefined && path.isAbsolute(this.env.DSH_HOME) ? this.env.DSH_HOME : path.join(os.homedir(), '.dsh');
    const candidates = this.env.FLOWX_SKILL_DIR ? [this.env.FLOWX_SKILL_DIR] : [DEFAULT_SKILL, path.join(home, 'skills', 'flowx-workflows'), path.join(process.cwd(), '.dsh', 'skills', 'flowx-workflows')];
    for (const candidate of candidates) {
      if (typeof candidate !== 'string' || !path.isAbsolute(candidate)) continue;
      try {
        const root = await fs.realpath(candidate);
        if ((await fs.stat(path.join(root, 'scripts', 'build.py'))).isFile()) return root;
      } catch { /* try the next location */ }
    }
    throw new Error();
  }

  // Only the build endpoints need the skill's build.py.
  async buildScript() {
    try {
      const root = await this.skillRoot();
      const script = await fs.realpath(path.join(root, 'scripts', 'build.py'));
      if (!inside(root, script) || !(await fs.stat(script)).isFile()) throw new Error();
      return script;
    } catch { throw problem('configuration', 'Building requires the FlowX skill (set FLOWX_SKILL_DIR or install it under ~/.dsh/skills/flowx-workflows).'); }
  }

  validSettings(settings) {
    return object(settings) && Object.keys(settings).every(key => key === 'apiKey' || SETTINGS_KEYS.includes(key)) &&
      SETTINGS_KEYS.every(key =>
        text(settings[key], 4096) && path.isAbsolute(settings[key]) && !/[\x00-\x1f\x7f]/.test(settings[key]) && this.redact(settings[key]) === settings[key]) &&
      (settings.apiKey === undefined || (text(settings.apiKey, 4096) && settings.apiKey.length > 0 && !/[\x00-\x1f\x7f]/.test(settings.apiKey))) &&
      Buffer.byteLength(JSON.stringify(settings) + '\n') <= SETTINGS_LIMIT;
  }

  settingsDirectory() {
    const home = this.env.DSH_HOME === undefined ? path.join(os.homedir(), '.dsh') : this.env.DSH_HOME;
    if (!text(home, 4096) || !path.isAbsolute(home) || /[\x00-\x1f\x7f]/.test(home) ||
        home.split(path.sep).some(part => part === '.' || part === '..')) throw storageError();
    return path.join(home, 'plugins', 'deepseek-harness-flowx');
  }

  async settingsDirectoryAccess(create) {
    const configured = this.settingsDirectory();
    // DSH_HOME is operator configuration and may itself pass through a
    // symlinked ancestor (for example /tmp -> /private/tmp on macOS). Resolve
    // the base once, then confine and check every component below it.
    const home = path.dirname(path.dirname(configured));
    let base;
    try { base = await fs.realpath(home); }
    catch (error) {
      if (error.code !== 'ENOENT') throw storageError();
      if (!create) return null;
      try { await fs.mkdir(home, { recursive: true, mode: 0o700 }); }
      catch { throw storageError(); }
      try { base = await fs.realpath(home); }
      catch { throw storageError(); }
    }
    const plugins = path.join(base, 'plugins');
    const directory = path.join(plugins, 'deepseek-harness-flowx');
    const root = path.parse(directory).root;
    let current = root;
    try {
      // Check every component rather than following an attacker-controlled link.
      for (const component of path.relative(root, directory).split(path.sep).filter(Boolean)) {
        current = path.join(current, component);
        let stat;
        try { stat = await fs.lstat(current); }
        catch (error) {
          if (error.code !== 'ENOENT') throw error;
          if (!create) return null;
          try { await fs.mkdir(current, { mode: 0o700 }); }
          catch (mkdirError) { if (mkdirError.code !== 'EEXIST') throw mkdirError; }
          stat = await fs.lstat(current);
        }
        if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error();
        // A replaceable ancestor would defeat lstat/O_NOFOLLOW checks on children.
        if (typeof process.getuid === 'function' && stat.uid !== 0 && stat.uid !== process.getuid()) throw new Error();
        if ((stat.mode & 0o022) && !(stat.mode & 0o1000)) throw new Error();
        if ((current === base || current === plugins || current === directory) && typeof process.getuid === 'function' && stat.uid !== process.getuid()) throw new Error();
        if ((current === base || current === plugins) && (stat.mode & 0o022)) throw new Error();
        if (current === directory && (stat.mode & 0o7777) !== 0o700) throw new Error();
      }
      if (await fs.realpath(directory) !== directory) throw new Error();
      return directory;
    } catch { throw storageError(); }
  }

  async readSettingsFile(directory) {
    const filename = path.join(directory, 'settings.json');
    let file;
    try {
      let before;
      try { before = await fs.lstat(filename); }
      catch (error) { if (error.code === 'ENOENT') return null; throw error; }
      if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1 || (before.mode & 0o7777) !== 0o600 ||
          (typeof process.getuid === 'function' && before.uid !== process.getuid()) || before.size > SETTINGS_LIMIT) throw new Error();
      file = await fs.open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW || 0) | (constants.O_NONBLOCK || 0));
      const opened = await file.stat();
      if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino || opened.nlink !== 1 ||
          (opened.mode & 0o7777) !== 0o600 || opened.size > SETTINGS_LIMIT) throw new Error();
      const buffer = Buffer.alloc(SETTINGS_LIMIT + 1);
      let offset = 0;
      while (offset < buffer.length) {
        const { bytesRead } = await file.read(buffer, offset, buffer.length - offset, offset);
        if (!bytesRead) break;
        offset += bytesRead;
      }
      const after = await fs.lstat(filename);
      if (offset > SETTINGS_LIMIT || !after.isFile() || after.dev !== opened.dev || after.ino !== opened.ino ||
          after.nlink !== 1 || (after.mode & 0o7777) !== 0o600 ||
          (typeof process.getuid === 'function' && after.uid !== process.getuid())) throw new Error();
      const settings = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, offset)));
      if (!this.validSettings(settings)) throw new Error();
      return settings;
    } catch { throw settingsError(); }
    finally { await file?.close().catch(() => {}); }
  }

  async loadSettings() {
    const directory = await this.settingsDirectoryAccess(false);
    const settings = directory ? await this.readSettingsFile(directory) : null;
    if (settings?.apiKey && !this.secrets.has(settings.apiKey)) {
      this.secrets.add(settings.apiKey);
      this.secretPattern = this.buildSecretPattern();
    }
    return settings;
  }

  async getSettings() {
    return this.withSharedLease(async () => {
      const saved = await this.loadSettings();
      return saved ? { python: saved.python, workspace: saved.workspace, apiKeySet: Boolean(saved.apiKey) || Boolean(this.env.FLOWX_LLM_API_KEY) } : this.fallbackSettings();
    });
  }

  saveSettings(submitted) {
    if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
    if (buildOwner || inspections) throw busy();
    // Reserve the lease synchronously, including while queued behind another save.
    settingsWrites++;
    const operation = settingsTail.then(async () => {
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      const previous = await this.loadSettings(); // Never overwrite a corrupt or unsafe existing file.
      // An empty key keeps the previously saved one.
      const apiKey = submitted.apiKey || previous?.apiKey;
      const settings = { python: submitted.python, workspace: submitted.workspace, ...(apiKey ? { apiKey } : {}) };
      if (!this.validSettings(settings)) throw problem('configuration', 'FlowX settings must contain safe absolute paths and a printable API key.');
      await this.validateConfig(settings, false);
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      const directory = await this.settingsDirectoryAccess(true);
      const filename = path.join(directory, 'settings.json');
      await this.readSettingsFile(directory);
      const temporary = path.join(directory, `.settings-${randomUUID()}.tmp`);
      let file, created = false;
      try {
        file = await fs.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW || 0), 0o600);
        created = true;
        await file.chmod(0o600);
        await file.writeFile(JSON.stringify(settings) + '\n', 'utf8');
        await file.sync();
        await file.close();
        file = null;
        // Recheck the directory and target immediately before atomic replacement.
        await this.settingsDirectoryAccess(false);
        await this.readSettingsFile(directory);
        if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
        await fs.rename(temporary, filename);
        created = false;
      } catch (error) {
        if (error instanceof HostError) throw error;
        throw storageError();
      } finally {
        await file?.close().catch(() => {});
        if (created) await fs.unlink(temporary).catch(() => {});
      }
      // Other service instances can share this settings file and cache the old root.
      for (const reference of services) {
        const service = reference.deref();
        if (!service) { services.delete(reference); continue; }
        service.artifacts.clear();
        service.graphSnapshot = null;
      }
      return { python: settings.python, workspace: settings.workspace, apiKeySet: Boolean(settings.apiKey) || Boolean(this.env.FLOWX_LLM_API_KEY) };
    });
    const completion = operation.finally(() => { settingsWrites--; this.settingsOps.delete(completion); });
    this.settingsOps.add(completion);
    settingsTail = completion.catch(() => {});
    return completion;
  }

  async config() {
    const saved = await this.loadSettings();
    return { ...await this.validateConfig(saved ?? this.fallbackSettings(), true), savedSettings: saved };
  }

  async validateConfig(settings, createStorage) {
    const { workspace, python } = settings;
    if (![workspace, python].every(p => typeof p === 'string' && path.isAbsolute(p))) {
      throw problem('configuration', 'FLOWX_WORKSPACE and FLOWX_PYTHON must be absolute paths.');
    }
    let stage = 'workspace directory';
    try {
      // Execute the configured Python path, not its realpath: a venv's symlink
      // must remain argv[0] so Python discovers pyvenv.cfg and its packages.
      const root = await fs.realpath(workspace);
      if (!(await fs.stat(root)).isDirectory()) throw new Error();
      stage = 'executable Python interpreter (FLOWX_PYTHON)';
      const interpreter = path.normalize(python);
      if (!(await fs.stat(interpreter)).isFile()) throw new Error();
      await fs.access(interpreter, constants.X_OK);
      stage = 'workspace storage (workflows/ must be a real directory without symlinks)';
      await this.checkStorage(root, createStorage);
      return { root, interpreter };
    } catch { throw problem('configuration', `FlowX configuration is invalid: cannot use ${stage}.`); }
  }

  async checkStorage(root, create = true) {
    // The SDK scans */agent.json even for list and before building a new workflow.
    // Refuse redirects there before handing the workspace to Python.
    const directory = path.join(root, 'workflows');
    if (create) await fs.mkdir(directory, { recursive: false }).catch(error => { if (error.code !== 'EEXIST') throw error; });
    else {
      try { await fs.lstat(directory); }
      catch (error) { if (error.code === 'ENOENT') return; throw error; }
    }
    if (await fs.realpath(directory) !== directory || !(await fs.stat(directory)).isDirectory()) throw new Error();
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) throw new Error();
      if (!entry.isDirectory()) continue;
      try {
        if ((await fs.lstat(path.join(directory, entry.name, 'agent.json'))).isSymbolicLink()) throw new Error();
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }

  spawnChild(command, args, settings) {
    // Keep Python, proxy and provider configuration, but drop unrelated secrets.
    // Credential paths and the explicitly supported FlowX/LLM keys are retained.
    const env = Object.fromEntries(Object.entries(this.env).filter(([key, value]) => {
      const upper = key.toUpperCase();
      if (PROVIDER_CREDENTIALS.has(upper) || CREDENTIAL_PATHS.has(upper) || PROVIDER_URLS.has(upper)) return true;
      return !SECRET_ENV.test(key) && !(typeof value === 'string' && URL_CREDENTIALS.test(value));
    }));
    if (settings) {
      Object.assign(env, { FLOWX_PYTHON: settings.python, FLOWX_WORKSPACE: settings.workspace });
      if (settings.apiKey) env.FLOWX_LLM_API_KEY = settings.apiKey;
    }
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
    if (buildOwner || inspections || settingsWrites) throw busy();
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
      const buildScript = await this.buildScript();
      const child = this.spawnChild(config.interpreter, [buildScript, '--workspace', config.root, 'new', '--host', '127.0.0.1', '--prompt-file', promptFile], config.savedSettings);
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

  async withSharedLease(operation) {
    if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
    if (settingsWrites || buildOwner) throw busy();
    inspections++;
    let finish;
    const completion = new Promise(resolve => { finish = resolve; });
    this.inspectionOps.add(completion);
    try {
      const result = await operation();
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      return result;
    } finally { inspections--; this.inspectionOps.delete(completion); finish(); }
  }

  async withInspection(operation) {
    return this.withSharedLease(async () => {
      const config = await this.config();
      if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
      return operation(config);
    });
  }

  async runInspection(config, operation, id) {
    if (this.closed) throw problem('disposed', 'The plugin has been disposed.');
    const args = ['-c', INSPECT_CODE, config.root, operation];
    if (id) args.push(id);
    const child = this.spawnChild(config.interpreter, args, config.savedSettings);
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

  async catalog(config) {
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
  }

  async list() {
    return this.withInspection(config => this.catalog(config));
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

  async readMapped(root, id, relative, graph = false, { browse = false } = {}) {
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
      if (!opened.isFile() || opened.size > LIMIT || (browse && opened.nlink !== 1) || opened.dev !== before.dev || opened.ino !== before.ino || await fs.realpath(candidate) !== real) throw new Error();
      const buffer = Buffer.alloc(LIMIT + 1);
      let offset = 0;
      while (offset < buffer.length) {
        const { bytesRead } = await file.read(buffer, offset, buffer.length - offset, offset);
        if (!bytesRead) break;
        offset += bytesRead;
      }
      if (offset > LIMIT) throw new Error();
      const source = new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, offset));
      if (browse && /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(source)) throw new Error();
      // All JSON and JSON-shaped text gets structural redaction, including graph
      // aliases and escaped credentials. Invalid JSON fails closed.
      const structured = graph || (browse && (path.extname(relative).toLowerCase() === '.json' || /^\s*[\[{]/.test(source)));
      let content = structured ? this.sanitizeGraph(source) : this.redact(source);
      if (browse && relative === 'agent.json') {
        const manifest = JSON.parse(content);
        if (!object(manifest)) throw new Error();
        if (Object.hasOwn(manifest, 'errors')) manifest.errors = 'Details withheld.';
        if (typeof manifest.main_entrypoint_path === 'string') manifest.main_entrypoint_path = '[withheld]';
        content = JSON.stringify(manifest);
      }
      if (!text(content)) throw new Error();
      return { workflowId: id, path: relative, content, bytes: offset, cached: false };
    } catch { throw problem('artifact-unavailable', 'The artifact is unavailable, unsafe, too large or not UTF-8 text.'); }
    finally { await file?.close().catch(() => {}); }
  }

  async readArtifact(id, relative) {
    if (settingsWrites) throw busy();
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

  safeFolderPath(relative) {
    return relative === '' || (relativeArtifact(relative) && relative.split('/').every(part =>
      !part.startsWith('.') && !SECRET_PATH.test(part) && this.safeIdentifier(part)));
  }

  folderGraph(root, id, relative) {
    const entry = this.artifacts.get(id);
    return (entry?.root === root && entry.map.graph === relative) ||
      (this.graphSnapshot?.workflowId === id && this.graphSnapshot.path === relative);
  }

  async folderWorkflow(config, id) {
    if (!this.safeIdentifier(id)) throw problem('artifact-unavailable', 'The workflow directory is unavailable or unsafe.');
    const directory = await this.workflowRoot(config.root, id);
    // The SDK scans */agent.json; verify this exact directory's regular marker
    // and its id before invoking it, then require actual catalog membership.
    const marker = await this.readMapped(config.root, id, 'agent.json', true, { browse: true });
    try { if (JSON.parse(marker.content).workflow_id !== id) throw new Error(); }
    catch { throw problem('artifact-unavailable', 'The workflow directory is unavailable or unsafe.'); }
    const { workflows } = await this.catalog(config);
    if (!Object.hasOwn(workflows, id)) throw problem('artifact-unavailable', 'The workflow directory is unavailable or unsafe.');
    return directory;
  }

  async folderDirectory(directory, relative) {
    try {
      const candidate = relative ? path.join(directory, ...relative.split('/')) : directory;
      if ((relative && !inside(directory, candidate)) || await fs.realpath(candidate) !== candidate || !(await fs.lstat(candidate)).isDirectory()) throw new Error();
      return candidate;
    } catch { throw problem('artifact-unavailable', 'The workflow directory is unavailable or unsafe.'); }
  }

  async listFolder(id, relative) {
    return this.withInspection(async config => {
      if (!this.safeFolderPath(relative)) throw problem('artifact-unavailable', 'The workflow directory is unavailable or unsafe.');
      const directory = await this.folderWorkflow(config, id);
      const target = await this.folderDirectory(directory, relative);
      const entries = [];
      let scanned = 0, metadata = 0, probed = 0, truncated = false;
      try {
        const before = await fs.lstat(target);
        const handle = await fs.opendir(target);
        for await (const item of handle) {
          if (++scanned > FOLDER_SCAN_LIMIT) { truncated = true; break; }
          const itemPath = relative ? `${relative}/${item.name}` : item.name;
          if (!this.safeFolderPath(itemPath) || item.isSymbolicLink()) continue;
          const candidate = path.join(target, item.name);
          let stat;
          try {
            stat = await fs.lstat(candidate);
            if (stat.isSymbolicLink() || (!stat.isDirectory() && !stat.isFile()) || await fs.realpath(candidate) !== candidate) continue;
          } catch { continue; }
          if (entries.length >= FOLDER_ENTRY_LIMIT) { truncated = true; break; }
          const isDirectory = stat.isDirectory();
          let readable = isDirectory;
          if (!isDirectory && stat.nlink === 1 && stat.size <= LIMIT && TEXT_TYPES.has(path.extname(item.name).toLowerCase())) {
            if (probed + stat.size > FOLDER_PROBE_LIMIT) truncated = true;
            else {
              probed += stat.size;
              try { await this.readMapped(config.root, id, itemPath, this.folderGraph(config.root, id, itemPath), { browse: true }); readable = true; }
              catch { /* Leave unsafe or invalid text disabled. */ }
            }
          }
          const entry = { name: item.name, path: itemPath, type: isDirectory ? 'directory' : 'file', readable };
          const bytes = Buffer.byteLength(JSON.stringify(entry)) + 1;
          if (metadata + bytes > FOLDER_RESPONSE_LIMIT) { truncated = true; break; }
          metadata += bytes;
          entries.push(entry);
        }
        // Detect ordinary directory replacement while enumerating. This is not
        // an OS sandbox or a lock against concurrent external workspace edits.
        await this.folderDirectory(directory, relative);
        const after = await fs.lstat(target);
        if (!after.isDirectory() || after.dev !== before.dev || after.ino !== before.ino) throw new Error();
      } catch (error) {
        if (error instanceof HostError) throw error;
        throw problem('artifact-unavailable', 'The workflow directory is unavailable or unsafe.');
      }
      entries.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
      return { workflowId: id, path: relative, entries, truncated };
    });
  }

  async readFolderFile(id, relative) {
    return this.withInspection(async config => {
      if (!this.safeFolderPath(relative)) throw problem('artifact-unavailable', 'The artifact is unavailable or unsafe.');
      await this.folderWorkflow(config, id);
      return this.readMapped(config.root, id, relative, this.folderGraph(config.root, id, relative), { browse: true });
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
      await Promise.allSettled([...this.settingsOps]);
      this.artifacts.clear();
      this.graphSnapshot = null;
      services.delete(this.serviceReference);
      serviceFinalizer.unregister(this.serviceReference);
      this.update({ status: 'disposed', requestId: null, awaiting: null, result: null, error: null });
    })();
    return this.disposePromise;
  }
}