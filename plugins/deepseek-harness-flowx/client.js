// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: DeepSeek Harness workflow client and graph explorer.
// Modified 2026-11-09: distinguish host instances and plans from published revisions; require build-risk consent.
// Modified 2026-11-09: call the authenticated shared Fetch route with stable injection.
// Modified 2026-11-09: give the graph explorer and repair prompt the full panel width.

window.__ModuleLoader__.load({
  id: 'dsh-flowx-plugin',
  factory(require) {
    const React = require('react');
    const { createElement: h, useCallback, useEffect, useRef, useState } = React;
    const activeStatuses = new Set(['starting', 'running', 'input_required', 'stopping', 'finishing', 'aborting']);
    const isActive = state => !!state && activeStatuses.has(state.status);
    const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
    const styles = {
      page: { color: '#192a44', background: '#f5f8fc', minHeight: '100%', padding: 'clamp(16px, 3vw, 36px)', fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', boxSizing: 'border-box' },
      shell: { maxWidth: 1440, margin: '0 auto', display: 'grid', gap: 20 },
      hero: { display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', alignItems: 'center', gap: 16 },
      eyebrow: { color: '#4d60cd', fontSize: 12, fontWeight: 800, letterSpacing: '.12em', textTransform: 'uppercase', margin: '0 0 6px' },
      title: { fontSize: 'clamp(26px, 3vw, 36px)', lineHeight: 1.15, margin: '0 0 8px', letterSpacing: '-.04em' },
      muted: { color: '#5d6e87', lineHeight: 1.5, fontSize: 14, margin: 0 },
      badge: { borderRadius: 999, background: '#e8edff', color: '#4051bd', padding: '8px 13px', fontSize: 13, fontWeight: 700 },
      card: { background: '#fff', border: '1px solid #e1e8f2', borderRadius: 16, padding: 20, boxShadow: '0 8px 28px rgba(37, 58, 95, .045)', minWidth: 0 },
      heading: { margin: '0 0 8px', fontSize: 17, letterSpacing: '-.02em' },
      sectionHead: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10, marginBottom: 14 },
      button: { border: '1px solid #d5dff0', background: '#fff', color: '#324864', borderRadius: 9, padding: '9px 13px', font: 'inherit', fontSize: 13, fontWeight: 700, cursor: 'pointer' },
      primary: { border: '1px solid #5366db', background: '#5366db', color: '#fff', borderRadius: 9, padding: '10px 16px', font: 'inherit', fontSize: 13, fontWeight: 700, cursor: 'pointer' },
      danger: { border: '1px solid #ecc8cd', background: '#fff7f7', color: '#b33d4b', borderRadius: 9, padding: '10px 16px', font: 'inherit', fontSize: 13, fontWeight: 700, cursor: 'pointer' },
      input: { width: '100%', boxSizing: 'border-box', border: '1px solid #cfd9e8', borderRadius: 10, padding: 12, color: '#192a44', font: 'inherit', lineHeight: 1.5, resize: 'vertical', background: '#fbfcff' },
      row: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 },
      grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 440px), 1fr))', gap: 20 },
      notice: { border: '1px solid #f0cbd0', color: '#953d4a', background: '#fff5f5', borderRadius: 10, padding: '11px 14px', fontSize: 14, overflowWrap: 'anywhere' },
      pre: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflow: 'auto', maxHeight: 420, background: '#15243a', color: '#e9f1ff', borderRadius: 10, padding: 14, fontSize: 12, lineHeight: 1.55 },
      label: { display: 'block', fontWeight: 700, fontSize: 13, margin: '0 0 7px' },
      divider: { border: 0, borderTop: '1px solid #e8edf5', margin: '18px 0' },
    };

    // Sort names rather than trusting graph order; longest-path layers are stable
    // for a DAG. Keep malformed references and cycles visible instead of hanging.
    function graphModel(graph) {
      if (!graph || !Array.isArray(graph.nodes)) throw new Error('The graph must contain a nodes array.');
      const byName = new Map();
      const warnings = [];
      for (const node of graph.nodes) {
        if (!node || typeof node.name !== 'string' || !node.name.trim()) {
          warnings.push('A node without a name was omitted.');
        } else if (byName.has(node.name)) {
          warnings.push(`Duplicate node: ${node.name}.`);
        } else {
          byName.set(node.name, { name: node.name, desc: typeof node.desc === 'string' ? node.desc : '', enabled: node.enable !== false, depends: Array.isArray(node.depends) ? [...new Set(node.depends.filter(dep => typeof dep === 'string'))].sort(compare) : [] });
        }
      }
      const names = [...byName.keys()].sort(compare);
      const outgoing = new Map(names.map(name => [name, []]));
      const degree = new Map(names.map(name => [name, 0]));
      const depth = new Map(names.map(name => [name, 0]));
      const edges = [];
      for (const name of names) {
        for (const dep of byName.get(name).depends) {
          if (!byName.has(dep)) { warnings.push(`${name} depends on missing node ${dep}.`); continue; }
          outgoing.get(dep).push(name);
          degree.set(name, degree.get(name) + 1);
          edges.push({ from: dep, to: name });
        }
      }
      const ready = names.filter(name => degree.get(name) === 0);
      let visited = 0;
      while (ready.length) {
        const name = ready.shift();
        visited++;
        for (const next of outgoing.get(name)) {
          depth.set(next, Math.max(depth.get(next), depth.get(name) + 1));
          degree.set(next, degree.get(next) - 1);
          if (degree.get(next) === 0) { ready.push(next); ready.sort(compare); }
        }
      }
      if (visited !== names.length) warnings.push('The graph contains a cycle; affected nodes may not follow dependency order.');
      const layers = new Map();
      for (const name of names) {
        const layer = depth.get(name);
        if (!layers.has(layer)) layers.set(layer, []);
        layers.get(layer).push(name);
      }
      const positions = new Map();
      let rows = 1, columns = 1;
      for (const [layer, members] of layers) {
        columns = Math.max(columns, layer + 1);
        rows = Math.max(rows, members.length);
        members.forEach((name, row) => positions.set(name, { x: 24 + layer * 270, y: 24 + row * 104 }));
      }
      return { nodes: names.map(name => ({ ...byName.get(name), ...positions.get(name) })), edges, warnings, width: 48 + (columns - 1) * 270 + 208, height: 48 + (rows - 1) * 104 + 68 };
    }

    function Icon() {
      return h('svg', { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': true },
        h('path', { d: 'M5 6h6v6H5zM13 12h6v6h-6zM11 9h4v3M5 15h4', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }),
        h('circle', { cx: 5, cy: 15, r: 1.5, fill: 'currentColor' }));
    }

    function Graph({ model, selected, onSelect }) {
      const positions = new Map(model.nodes.map(node => [node.name, node]));
      return h('div', null,
        model.warnings.length > 0 && h('div', { role: 'status', style: { ...styles.notice, marginBottom: 12 } }, model.warnings.join(' ')),
        model.nodes.length === 0 ? h('p', { style: styles.muted }, 'This graph has no nodes.') :
          h('div', { style: { overflowX: 'auto', border: '1px solid #edf1f8', borderRadius: 12, background: '#f8faff' } },
            h('svg', { viewBox: `0 0 ${model.width} ${model.height}`, width: model.width, height: model.height, role: 'group', 'aria-label': 'Current plan dependency graph. Select a node to inspect it.', style: { display: 'block', maxWidth: 'none' } },
              model.edges.map(edge => {
                const from = positions.get(edge.from), to = positions.get(edge.to);
                const x1 = from.x + 208, y1 = from.y + 34, x2 = to.x, y2 = to.y + 34;
                const bend = Math.max(32, Math.abs(x2 - x1) / 2);
                return h('g', { key: `${edge.from}\0${edge.to}`, 'aria-hidden': true, 'data-flowx-edge': `${edge.from} → ${edge.to}` },
                  h('path', { d: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`, stroke: '#9eade3', strokeWidth: 2, fill: 'none' }),
                  h('path', { d: `M ${x2 - 7} ${y2 - 4} L ${x2} ${y2} L ${x2 - 7} ${y2 + 4}`, stroke: '#9eade3', strokeWidth: 2, fill: 'none' }));
              }),
              model.nodes.map(node => h('g', {
                key: node.name, role: 'button', tabIndex: 0, focusable: true, 'aria-label': `Inspect node ${node.name}${node.enabled ? '' : ' (disabled)'}`, 'aria-pressed': selected === node.name,
                'data-flowx-node': node.name, onClick: () => onSelect(node.name),
                onKeyDown: event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(node.name); } },
                onFocus: event => { event.currentTarget.style.outline = '2px solid #5366db'; },
                onBlur: event => { event.currentTarget.style.outline = ''; },
                style: { cursor: 'pointer' },
              },
              h('title', null, `${node.name}${node.enabled ? '' : ' (disabled)'}`),
              h('rect', { x: node.x, y: node.y, width: 208, height: 68, rx: 12, fill: selected === node.name ? '#eef0ff' : '#fff', stroke: selected === node.name ? '#6071df' : '#dce4f2', strokeWidth: selected === node.name ? 2 : 1 }),
              h('circle', { cx: node.x + 19, cy: node.y + 22, r: 5, fill: node.enabled ? '#7181eb' : '#8a6470' }),
              h('text', { x: node.x + 32, y: node.y + 27, fill: '#223451', fontSize: 13, fontWeight: 700 }, node.name.length > 23 ? `${node.name.slice(0, 22)}…` : node.name),
              h('text', { x: node.x + 19, y: node.y + 49, fill: '#5d6e87', fontSize: 11 }, `${node.depends.length} ${node.depends.length === 1 ? 'dependency' : 'dependencies'}${node.enabled ? '' : ' · Disabled'}`))))));
    }

    async function callFlowX(endpoint, payload) {
      let response;
      try {
        response = await fetch('api/flowx', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ endpoint, payload }), credentials: 'same-origin',
        });
      } catch {
        throw new Error('Unable to reach FlowX.');
      }
      let result;
      try { result = await response.json(); } catch { /* Harness errors can be plain text. */ }
      if (!response.ok || !result || result.ok !== true || !Object.hasOwn(result, 'value')) {
        const message = typeof result?.error?.message === 'string' && result.error.message
          ? result.error.message : !response.ok ? `The FlowX request failed (HTTP ${response.status}).` : 'The FlowX response is invalid.';
        const failure = new Error(message);
        if (typeof result?.error?.code === 'string') failure.code = result.error.code;
        throw failure;
      }
      return result.value;
    }

    function Page({ call }) {
      const [state, setState] = useState(null);
      const [error, setError] = useState('');
      const [prompt, setPrompt] = useState('');
      const [consent, setConsent] = useState(false);
      const [answer, setAnswer] = useState('');
      const [operation, setOperation] = useState('');
      const [catalog, setCatalog] = useState(null);
      const [catalogVersion, setCatalogVersion] = useState(0);
      const [workflow, setWorkflow] = useState(null);
      const [artifacts, setArtifacts] = useState(null);
      const [graph, setGraph] = useState(null);
      const [graphError, setGraphError] = useState('');
      const [selectedNode, setSelectedNode] = useState(null);
      const [viewer, setViewer] = useState(null);
      const mounted = useRef(false);
      const current = useRef(null);
      const stateRequests = useRef(0);
      const acceptedRequest = useRef(0);
      const stateEpoch = useRef(0);
      const retiredInstances = useRef(new Set());
      const operationRef = useRef('');
      const selection = useRef(0);
      const pendingList = useRef(false);
      const busy = !!operation;
      const active = isActive(state);

      const applyState = useCallback((snapshot, ticket, epoch) => {
        if (!mounted.current) return;
        if (!snapshot || !Number.isSafeInteger(snapshot.revision) || typeof snapshot.status !== 'string' || (snapshot.instanceId != null && (typeof snapshot.instanceId !== 'string' || !snapshot.instanceId.trim()))) throw new Error('The build status response is invalid.');
        // Older hosts omit instanceId; they remain usable but cannot detect restarts.
        const instance = snapshot.instanceId ?? null;
        const previous = current.current;
        const previousInstance = previous?.instanceId ?? null;
        // Never let an unidentifiable response replace an identified host.
        if (previousInstance !== null && instance === null) return;
        if (previous && instance === previousInstance) {
          if (snapshot.revision < previous.revision || (snapshot.revision === previous.revision && ticket < acceptedRequest.current)) return;
        } else {
          // A request sent before the current instance was observed cannot switch
          // instances, even if it happens to complete later than that response.
          if (epoch !== stateEpoch.current || ticket < acceptedRequest.current || retiredInstances.current.has(instance)) return;
          if (previous) {
            stateEpoch.current++;
            retiredInstances.current.add(previousInstance);
            selection.current++;
            setCatalog(null); setWorkflow(null); setArtifacts(null); setGraph(null); setGraphError(''); setSelectedNode(null); setViewer(null);
            setCatalogVersion(value => value + 1);
          }
        }
        acceptedRequest.current = Math.max(acceptedRequest.current, ticket);
        current.current = snapshot;
        setState(snapshot);
      }, []);
      const requestState = useCallback(async (endpoint, payload) => {
        const ticket = ++stateRequests.current;
        const epoch = stateEpoch.current;
        try { applyState(await call(endpoint, payload), ticket, epoch); }
        catch (failure) {
          if (epoch === stateEpoch.current && (endpoint !== 'status' || ticket >= acceptedRequest.current)) throw failure;
        }
      }, [call, applyState]);
      const refreshStatus = useCallback(async () => {
        try { await requestState('status', null); }
        catch (failure) { if (mounted.current) setError(failure.message || 'Unable to load status.'); }
      }, [requestState]);
      const begin = kind => {
        if (operationRef.current) return false;
        operationRef.current = kind;
        setOperation(kind);
        return true;
      };
      const end = kind => {
        if (operationRef.current === kind) operationRef.current = '';
        const reload = pendingList.current && !isActive(current.current);
        if (reload) pendingList.current = false;
        if (mounted.current) {
          setOperation('');
          if (reload) setCatalogVersion(value => value + 1);
        }
        // A StrictMode cleanup/setup can replay effects before an in-flight
        // list settles. Retain the request to reload across that boundary.
        else if (reload) pendingList.current = true;
      };

      useEffect(() => {
        mounted.current = true;
        if (pendingList.current && !operationRef.current && !isActive(current.current)) {
          pendingList.current = false;
          setCatalogVersion(value => value + 1);
        }
        return () => { mounted.current = false; selection.current++; };
      }, []);
      useEffect(() => { refreshStatus(); }, [refreshStatus]);
      useEffect(() => {
        if (!active) return undefined;
        let cancelled = false, timer;
        const tick = async () => {
          await refreshStatus();
          if (!cancelled) timer = setTimeout(tick, 2000);
        };
        timer = setTimeout(tick, 2000);
        return () => { cancelled = true; clearTimeout(timer); };
      }, [active, refreshStatus]);
      useEffect(() => { setAnswer(''); }, [state?.instanceId, state?.requestId, state?.awaiting?.stepId, state?.revision]);
      useEffect(() => {
        if (!active) return;
        selection.current++;
        setWorkflow(null); setArtifacts(null); setGraph(null); setGraphError(''); setSelectedNode(null); setViewer(null);
      }, [active]);
      useEffect(() => {
        if (!state || active) return undefined;
        if (!begin('list')) { pendingList.current = true; return undefined; }
        pendingList.current = false;
        const epoch = stateEpoch.current;
        let cancelled = false, retry;
        call('list', null).then(value => {
          if (!cancelled && mounted.current && epoch === stateEpoch.current && !isActive(current.current)) setCatalog(value?.workflows || {});
        }).catch(failure => {
          if (cancelled || !mounted.current || epoch !== stateEpoch.current || isActive(current.current)) return;
          if (failure.code === 'flowx/busy') retry = setTimeout(() => setCatalogVersion(value => value + 1), 2000);
          else setError(failure.message || 'Unable to list workflows.');
        }).finally(() => end('list'));
        return () => { cancelled = true; clearTimeout(retry); };
      }, [!!state, active, catalogVersion, call]);

      const start = async event => {
        event.preventDefault();
        if (!current.current || isActive(current.current) || !prompt.trim() || !consent || !begin('start')) return;
        setConsent(false);
        setError('');
        try { await requestState('start', { prompt }); }
        catch (failure) {
          if (mounted.current) setError(failure.message || 'Unable to start the build.');
          await refreshStatus();
        }
        finally { end('start'); }
      };
      const respond = async (abort, event) => {
        if (event) event.preventDefault();
        const snapshot = current.current;
        if (snapshot?.status !== 'input_required' || !snapshot.requestId || !snapshot.awaiting?.stepId || !begin(abort ? 'abort' : 'answer')) return;
        const kind = abort ? 'abort' : 'answer';
        const payload = { requestId: snapshot.requestId, stepId: snapshot.awaiting.stepId, revision: snapshot.revision };
        if (!abort) payload.input = answer;
        setError('');
        try { await requestState(kind, payload); }
        catch (failure) {
          if (failure.code === 'flowx/stale-input') {
            await refreshStatus();
            if (mounted.current) setError('The repair request changed. Review the current request before trying again.');
          } else {
            if (mounted.current) setError(failure.message || 'Unable to submit the repair response.');
            await refreshStatus();
          }
        } finally { end(kind); }
      };
      const inspect = async id => {
        if (isActive(current.current) || !begin('inspect')) return;
        const ticket = ++selection.current;
        setWorkflow(id); setArtifacts(null); setGraph(null); setGraphError(''); setSelectedNode(null); setViewer(null); setError('');
        try {
          const result = await call('inspect', { workflowId: id });
          if (!mounted.current || ticket !== selection.current) return;
          const map = result?.artifacts;
          if (!map || typeof map !== 'object') throw new Error('The workflow artifact map is invalid.');
          setArtifacts(map);
          if (map.graph) {
            try {
              const file = await call('readArtifact', { workflowId: id, path: map.graph });
              const model = graphModel(JSON.parse(file.content));
              if (mounted.current && ticket === selection.current) setGraph(model);
            } catch (failure) {
              if (mounted.current && ticket === selection.current) setGraphError(`Unable to load graph: ${failure.message || 'Invalid graph.'}`);
            }
          }
        } catch (failure) { if (mounted.current && ticket === selection.current) setError(failure.message || 'Unable to inspect workflow.'); }
        finally { end('inspect'); }
      };
      const readArtifact = async (path, label) => {
        if (!path || !workflow || isActive(current.current) || !begin('artifact')) return;
        const ticket = selection.current;
        setViewer({ label, path, loading: true }); setError('');
        try {
          const file = await call('readArtifact', { workflowId: workflow, path });
          if (mounted.current && ticket === selection.current) setViewer({ label, path, content: file.content, bytes: file.bytes });
        } catch (failure) {
          if (mounted.current && ticket === selection.current) setViewer({ label, path, error: failure.message || 'Unable to read artifact.' });
        } finally { end('artifact'); }
      };
      const artifactButton = (label, path) => path
        ? h('button', { type: 'button', key: label, style: styles.button, disabled: busy || active, onClick: () => readArtifact(path, label) }, label)
        : h('span', { key: label, style: styles.muted }, `${label} unavailable`);
      const node = graph?.nodes.find(item => item.name === selectedNode);
      const nodeArtifacts = selectedNode && artifacts?.nodes?.[selectedNode];
      const workflows = catalog && typeof catalog === 'object' ? Object.entries(catalog).sort(([a], [b]) => compare(a, b)) : [];

      return h('main', { style: styles.page }, h('div', { style: styles.shell },
        h('header', { style: styles.hero }, h('div', null,
          h('p', { style: styles.eyebrow }, 'FlowX · workflow studio'),
          h('h1', { style: styles.title }, 'Build what flows next.'),
          h('p', { style: styles.muted }, 'Create workflows, resolve repairs, and explore every artifact in one place.')),
          h('div', { style: styles.row }, h('span', { style: styles.badge, role: 'status' }, state ? `● ${state.status.replaceAll('_', ' ')}` : '● Connecting'),
            h('button', { type: 'button', style: styles.button, onClick: () => { setError(''); refreshStatus(); } }, 'Refresh status'))),
        error && h('div', { role: 'alert', style: styles.notice }, error),
        state?.error?.message && h('div', { role: 'alert', style: styles.notice }, state.error.message),
        state && state.instanceId == null && h('div', { role: 'status', style: styles.notice }, 'This host does not provide an instance ID; automatic restart detection is unavailable.'),
        h('div', { style: styles.grid },
          h('section', { style: styles.card, 'aria-labelledby': 'flowx-build-heading' },
            h('h2', { id: 'flowx-build-heading', style: styles.heading }, 'Start a build'),
            h('p', { style: styles.muted }, 'Describe the workflow you want to create.'),
            h('form', { onSubmit: start, style: { marginTop: 16 } },
              h('label', { htmlFor: 'flowx-prompt', style: styles.label }, 'Workflow prompt'),
              h('textarea', { id: 'flowx-prompt', style: styles.input, rows: 4, value: prompt, onChange: event => setPrompt(event.target.value), placeholder: 'Describe your goal, inputs, and desired output…', disabled: active }),
              h('p', { id: 'flowx-build-risk', style: { ...styles.notice, margin: '12px 0' } }, 'Builds run generated code and pytest without a sandbox, with host privileges. API credentials, local files, and network access may be exposed. Prompts, repair context, and artifacts may contain sensitive data.'),
              h('div', { style: { display: 'flex', alignItems: 'flex-start', gap: 8 } },
                h('input', { id: 'flowx-build-consent', type: 'checkbox', checked: consent, onChange: event => setConsent(event.target.checked), disabled: active || busy, required: true, 'aria-describedby': 'flowx-build-risk' }),
                h('label', { htmlFor: 'flowx-build-consent', style: { ...styles.label, margin: 0 } }, 'I understand and accept these risks for this build.')),
              h('div', { style: { ...styles.row, marginTop: 12 } },
                h('button', { type: 'submit', style: styles.primary, disabled: !state || active || busy || !prompt.trim() || !consent }, operation === 'start' ? 'Starting…' : 'Build workflow'),
                state?.workflowId && h('span', { style: styles.muted }, `Workflow: ${state.workflowId}`))),
            active && state?.status !== 'input_required' && h('p', { style: { ...styles.muted, marginTop: 12 } }, 'Build in progress. Abort is available if a repair prompt appears.')),
          h('section', { style: styles.card, 'aria-labelledby': 'flowx-workflows-heading' },
            h('div', { style: styles.sectionHead }, h('div', null, h('h2', { id: 'flowx-workflows-heading', style: styles.heading }, 'Your workflows'), h('p', { style: styles.muted }, 'Select a workflow to explore its graph and files.')),
              h('button', { type: 'button', style: styles.button, disabled: !state || active || busy, onClick: () => { setError(''); setCatalogVersion(value => value + 1); } }, 'Refresh')),
            operation === 'list' ? h('p', { role: 'status', style: styles.muted }, 'Loading workflows…') :
              active ? h('p', { style: styles.muted }, 'Workflow inspection is unavailable while a build is active.') :
                catalog === null ? h('p', { style: styles.muted }, 'Workflows are not loaded yet.') : workflows.length === 0 ? h('p', { style: styles.muted }, 'No workflows yet. Start a build to create one.') :
                  h('div', { style: { display: 'grid', gap: 8 } }, workflows.map(([id, info]) => h('button', { key: id, type: 'button', style: { ...styles.button, textAlign: 'left', background: workflow === id ? '#f0f3ff' : '#fff' }, disabled: busy, 'aria-pressed': workflow === id, onClick: () => inspect(id) },
                    h('span', { style: { display: 'block', overflowWrap: 'anywhere' } }, id),
                    h('span', { style: { ...styles.muted, display: 'block', fontWeight: 400, fontSize: 12, marginTop: 4 } }, `Published workflow revision ${info?.workflow_version ?? '—'} · ${Object.keys(info?.nodes || {}).length} listed nodes`))))),
        state?.status === 'input_required' && state.awaiting && h('section', { style: { ...styles.card, gridColumn: '1 / -1', borderColor: '#f0d7a4', background: '#fffcf5' }, 'aria-labelledby': 'flowx-repair-heading' },
          h('h2', { id: 'flowx-repair-heading', style: styles.heading }, 'Repair needs your input'),
          h('p', { style: styles.muted }, `Step: ${state.awaiting.stepId}`),
          state.awaiting.prompt && h('p', { style: { whiteSpace: 'pre-wrap', lineHeight: 1.5 } }, state.awaiting.prompt),
          state.awaiting.test_log && h('details', null, h('summary', null, 'Test log'), h('pre', { style: styles.pre }, state.awaiting.test_log)),
          h('form', { onSubmit: event => respond(false, event), style: { marginTop: 14 } },
            h('label', { htmlFor: 'flowx-answer', style: styles.label }, 'Repair input'),
            h('textarea', { id: 'flowx-answer', style: styles.input, rows: 3, value: answer, onChange: event => setAnswer(event.target.value), placeholder: 'Provide guidance, or leave empty to continue.' }),
            h('div', { style: { ...styles.row, marginTop: 12 } },
              h('button', { type: 'submit', style: styles.primary, disabled: busy }, operation === 'answer' ? 'Sending…' : 'Continue repair'),
              h('button', { type: 'button', style: styles.danger, disabled: busy, onClick: () => respond(true) }, operation === 'abort' ? 'Aborting…' : 'Abort build')))),
        workflow && h('section', { style: { ...styles.card, gridColumn: '1 / -1' }, 'aria-labelledby': 'flowx-explorer-heading' },
          h('div', { style: styles.sectionHead }, h('div', null, h('p', { style: styles.eyebrow }, 'Workflow explorer'), h('h2', { id: 'flowx-explorer-heading', style: { ...styles.heading, overflowWrap: 'anywhere' } }, workflow)),
            artifacts && h('span', { style: styles.badge }, `Published workflow revision ${artifacts.workflow_version ?? '—'}`)),
          operation === 'inspect' && h('p', { role: 'status', style: styles.muted }, 'Loading workflow and graph…'),
          artifacts && h(React.Fragment, null,
            h('div', { style: styles.row }, artifactButton('Requirement', artifacts.requirement), artifactButton('Main entrypoint', artifacts.main_entrypoint?.path), artifactButton('Graph JSON', artifacts.graph)),
            h('hr', { style: styles.divider }),
            h('h3', { style: styles.heading }, 'Current plan dependency graph'),
            h('p', { style: { ...styles.muted, marginBottom: 12 } }, 'This graph shows the current plan. Published workflow and node revisions and artifacts may differ after graph-only updates. Select a node to inspect it.'),
            graphError ? h('div', { role: 'alert', style: styles.notice }, graphError) : graph ? h(Graph, { model: graph, selected: selectedNode, onSelect: setSelectedNode }) : !artifacts.graph ? h('p', { style: styles.muted }, 'No graph artifact is available.') : null,
            node && h('div', { style: { marginTop: 16, padding: 16, border: '1px solid #e3e9f5', borderRadius: 12 } },
              h('h3', { style: styles.heading }, node.name),
              h('p', { style: { ...styles.muted, whiteSpace: 'pre-wrap' } }, node.desc || 'No description provided.'),
              h('p', { style: { ...styles.muted, marginTop: 8 } }, `Status in current plan: ${node.enabled ? 'Enabled' : 'Disabled'}`),
              h('p', { style: { ...styles.muted, marginTop: 8 } }, `Depends on: ${node.depends.length ? node.depends.join(', ') : 'None'}`),
              nodeArtifacts && h('p', { style: { ...styles.muted, marginTop: 4 } }, `Node ID: ${nodeArtifacts.node_id} · Published node revision ${nodeArtifacts.version}`),
              h('div', { style: { ...styles.row, marginTop: 12 } }, nodeArtifacts ? Object.entries(nodeArtifacts.artifacts || {}).sort(([a], [b]) => compare(a, b)).map(([label, path]) => artifactButton(label, path)) : h('span', { style: styles.muted }, 'No generated node artifacts.'))),
            viewer && h('div', { style: { marginTop: 20 } },
              h('h3', { style: styles.heading }, viewer.label),
              h('p', { style: { ...styles.muted, overflowWrap: 'anywhere' } }, viewer.path, viewer.bytes != null ? ` · ${viewer.bytes} bytes` : ''),
              viewer.loading ? h('p', { role: 'status', style: styles.muted }, 'Loading artifact…') : viewer.error ? h('div', { role: 'alert', style: styles.notice }, viewer.error) : h('pre', { style: styles.pre, tabIndex: 0 }, viewer.content)))))));
    }

    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'flowx', inject: () => ({ call: callFlowX }) }, Page));
        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: 'flowx', order: 100, label: 'FlowX' }, Icon));
      },
    };
  },
});