// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-09: DeepSeek Harness workflow client and graph explorer.
// Modified 2026-11-09: distinguish host instances and plans from published revisions; require build-risk consent.
// Modified 2026-11-09: call the authenticated shared Fetch route with stable injection.
// Modified 2026-11-09: give the graph explorer and repair prompt the full panel width.
// Modified 2026-11-10: replace the build page with a read-only left overlay, path settings, and folder browser.
// Modified 2026-11-10: dismiss the narrow navigation drawer before opening the overlay.

window.__ModuleLoader__.load({
  id: 'dsh-flowx-plugin',
  factory(require) {
    const React = require('react');
    const { createElement: h, useCallback, useEffect, useRef, useState } = React;
    const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
    const styles = {
      panel: { pointerEvents: 'auto', position: 'absolute', top: 8, bottom: 8, left: 8, width: 'min(600px, calc(100vw - 16px))', boxSizing: 'border-box', overflowY: 'auto', background: '#f5f8fc', color: '#192a44', border: '1px solid #dce5f3', borderRadius: 16, boxShadow: '0 20px 70px rgba(17, 33, 61, .25)', fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', fontSize: 14 },
      content: { padding: 18, display: 'grid', gap: 14 },
      header: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12 },
      title: { margin: '0 0 4px', fontSize: 22, letterSpacing: '-.04em' },
      eyebrow: { color: '#5366cc', fontSize: 11, fontWeight: 800, letterSpacing: '.12em', textTransform: 'uppercase', margin: '0 0 5px' },
      muted: { color: '#5d6e87', lineHeight: 1.5, fontSize: 13, margin: 0 },
      card: { background: '#fff', border: '1px solid #e1e8f2', borderRadius: 13, padding: 16, minWidth: 0 },
      heading: { margin: '0 0 8px', fontSize: 16, letterSpacing: '-.02em' },
      row: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 },
      button: { border: '1px solid #d5dff0', background: '#fff', color: '#324864', borderRadius: 8, padding: '7px 10px', font: 'inherit', fontSize: 13, fontWeight: 700, cursor: 'pointer' },
      primary: { border: '1px solid #5366db', background: '#5366db', color: '#fff', borderRadius: 8, padding: '8px 12px', font: 'inherit', fontSize: 13, fontWeight: 700, cursor: 'pointer' },
      input: { width: '100%', boxSizing: 'border-box', border: '1px solid #cfd9e8', borderRadius: 8, padding: 10, color: '#192a44', font: 'inherit', background: '#fbfcff' },
      label: { display: 'block', fontWeight: 700, fontSize: 12, margin: '14px 0 6px' },
      notice: { border: '1px solid #f0cbd0', color: '#953d4a', background: '#fff5f5', borderRadius: 9, padding: '10px 12px', fontSize: 13, overflowWrap: 'anywhere' },
      pre: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflow: 'auto', maxHeight: 380, background: '#15243a', color: '#e9f1ff', borderRadius: 9, padding: 12, fontSize: 12, lineHeight: 1.5 },
    };

    function graphModel(graph) {
      if (!graph || !Array.isArray(graph.nodes)) throw new Error('The graph must contain a nodes array.');
      const byName = new Map(), warnings = [];
      for (const node of graph.nodes) {
        if (!node || typeof node.name !== 'string' || !node.name.trim()) warnings.push('A node without a name was omitted.');
        else if (byName.has(node.name)) warnings.push(`Duplicate node: ${node.name}.`);
        else byName.set(node.name, { name: node.name, desc: typeof node.desc === 'string' ? node.desc : '', enabled: node.enable !== false, depends: Array.isArray(node.depends) ? [...new Set(node.depends.filter(dep => typeof dep === 'string'))].sort(compare) : [] });
      }
      const names = [...byName.keys()].sort(compare);
      const outgoing = new Map(names.map(name => [name, []]));
      const degree = new Map(names.map(name => [name, 0]));
      const depth = new Map(names.map(name => [name, 0]));
      const edges = [];
      for (const name of names) for (const dep of byName.get(name).depends) {
        if (!byName.has(dep)) { warnings.push(`${name} depends on missing node ${dep}.`); continue; }
        outgoing.get(dep).push(name); degree.set(name, degree.get(name) + 1); edges.push({ from: dep, to: name });
      }
      const ready = names.filter(name => degree.get(name) === 0);
      let visited = 0;
      while (ready.length) {
        const name = ready.shift(); visited++;
        for (const next of outgoing.get(name)) {
          depth.set(next, Math.max(depth.get(next), depth.get(name) + 1));
          degree.set(next, degree.get(next) - 1);
          if (degree.get(next) === 0) { ready.push(next); ready.sort(compare); }
        }
      }
      if (visited !== names.length) warnings.push('The graph contains a cycle; affected nodes may not follow dependency order.');
      const layers = new Map(), positions = new Map();
      for (const name of names) { const layer = depth.get(name); if (!layers.has(layer)) layers.set(layer, []); layers.get(layer).push(name); }
      let rows = 1, columns = 1;
      for (const [layer, members] of layers) {
        columns = Math.max(columns, layer + 1); rows = Math.max(rows, members.length);
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
        model.warnings.length > 0 && h('div', { role: 'status', style: { ...styles.notice, marginBottom: 10 } }, model.warnings.join(' ')),
        model.nodes.length === 0 ? h('p', { style: styles.muted }, 'This graph has no nodes.') :
          h('div', { style: { overflow: 'auto', maxHeight: 450, border: '1px solid #edf1f8', borderRadius: 10, background: '#f8faff' } },
            h('svg', { viewBox: `0 0 ${model.width} ${model.height}`, width: model.width, height: model.height, role: 'group', 'aria-label': 'Current plan dependency graph. Select a node to inspect it.', style: { display: 'block', maxWidth: 'none' } },
              model.edges.map(edge => {
                const from = positions.get(edge.from), to = positions.get(edge.to);
                const x1 = from.x + 208, y1 = from.y + 34, x2 = to.x, y2 = to.y + 34, bend = Math.max(32, Math.abs(x2 - x1) / 2);
                return h('g', { key: `${edge.from}\0${edge.to}`, 'aria-hidden': true, 'data-flowx-edge': `${edge.from} → ${edge.to}` },
                  h('path', { d: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`, stroke: '#9eade3', strokeWidth: 2, fill: 'none' }),
                  h('path', { d: `M ${x2 - 7} ${y2 - 4} L ${x2} ${y2} L ${x2 - 7} ${y2 + 4}`, stroke: '#9eade3', strokeWidth: 2, fill: 'none' }));
              }),
              model.nodes.map(node => h('g', { key: node.name, role: 'button', tabIndex: 0, focusable: true, 'aria-label': `Inspect node ${node.name}${node.enabled ? '' : ' (disabled)'}`, 'aria-pressed': selected === node.name, 'data-flowx-node': node.name, onClick: () => onSelect(node.name), onKeyDown: event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(node.name); } }, style: { cursor: 'pointer' } },
                h('title', null, `${node.name}${node.enabled ? '' : ' (disabled)'}`),
                h('rect', { x: node.x, y: node.y, width: 208, height: 68, rx: 12, fill: selected === node.name ? '#eef0ff' : '#fff', stroke: selected === node.name ? '#6071df' : '#dce4f2', strokeWidth: selected === node.name ? 2 : 1 }),
                h('circle', { cx: node.x + 19, cy: node.y + 22, r: 5, fill: node.enabled ? '#7181eb' : '#8a6470' }),
                h('text', { x: node.x + 32, y: node.y + 27, fill: '#223451', fontSize: 13, fontWeight: 700 }, node.name.length > 23 ? `${node.name.slice(0, 22)}…` : node.name),
                h('text', { x: node.x + 19, y: node.y + 49, fill: '#5d6e87', fontSize: 11 }, `${node.depends.length} ${node.depends.length === 1 ? 'dependency' : 'dependencies'}${node.enabled ? '' : ' · Disabled'}`))))));
    }

    async function callFlowX(endpoint, payload) {
      let response;
      try { response = await fetch('api/flowx', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ endpoint, payload }), credentials: 'same-origin' }); }
      catch { throw new Error('Unable to reach FlowX.'); }
      let result;
      try { result = await response.json(); } catch { /* Harness errors can be plain text. */ }
      if (!response.ok || !result || result.ok !== true || !Object.hasOwn(result, 'value')) {
        const failure = new Error(typeof result?.error?.message === 'string' && result.error.message ? result.error.message : !response.ok ? `The FlowX request failed (HTTP ${response.status}).` : 'The FlowX response is invalid.');
        if (typeof result?.error?.code === 'string') failure.code = result.error.code;
        throw failure;
      }
      return result.value;
    }

    function Folder({ path, folders, onToggle, onRead }) {
      const folder = folders[path];
      if (!folder) return null;
      if (folder.loading) return h('p', { role: 'status', style: styles.muted }, 'Loading folder…');
      if (folder.error) return h('div', null, h('p', { role: 'alert', style: styles.notice }, folder.error), h('button', { type: 'button', style: styles.button, onClick: () => onToggle(path, true) }, 'Retry folder'));
      return h('div', { style: { display: 'grid', gap: 3 } },
        folder.entries.length === 0 && h('p', { style: styles.muted }, 'No visible files.'),
        folder.entries.map(entry => h('div', { key: entry.path },
          h('button', { type: 'button', disabled: !entry.readable, 'aria-expanded': entry.type === 'directory' ? !!folders[entry.path] : undefined, 'data-flowx-path': entry.path, onClick: () => entry.type === 'directory' ? onToggle(entry.path) : onRead(entry.path), style: { ...styles.button, width: '100%', border: 0, textAlign: 'left', overflowWrap: 'anywhere', opacity: entry.readable ? 1 : .55, background: '#f8faff' } }, `${entry.type === 'directory' ? folders[entry.path] ? '▾' : '▸' : '·'} ${entry.name}${entry.readable ? '' : ' (unavailable)'}`),
          entry.type === 'directory' && folders[entry.path] && h('div', { style: { marginLeft: 14, borderLeft: '1px solid #e2e8f1', paddingLeft: 8 } }, h(Folder, { path: entry.path, folders, onToggle, onRead })))),
        folder.truncated && h('p', { role: 'status', style: styles.muted }, 'Folder listing is limited; some entries or readable files may be omitted.'));
    }

    function Page({ call, close, subscribeSaved, notifySaved }) {
      const [catalog, setCatalog] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState('');
      const [workflow, setWorkflow] = useState(null), [artifacts, setArtifacts] = useState(null), [graph, setGraph] = useState(null), [graphError, setGraphError] = useState('');
      const [selectedNode, setSelectedNode] = useState(null), [viewer, setViewer] = useState(null), [folders, setFolders] = useState({});
      const [settings, setSettings] = useState(null), [settingsError, setSettingsError] = useState(''), [settingsBusy, setSettingsBusy] = useState(false);
      const mounted = useRef(false), generation = useRef(0), listTicket = useRef(0), viewTicket = useRef(0), folderTickets = useRef({}), settingsTicket = useRef(0), operation = useRef(''), saving = useRef(false);
      const closeRef = useRef(null), settingsRef = useRef(null), settingsButton = useRef(null);
      const clearExplorer = useCallback(() => {
        generation.current++; viewTicket.current++; folderTickets.current = {};
        setWorkflow(null); setArtifacts(null); setGraph(null); setGraphError(''); setSelectedNode(null); setViewer(null); setFolders({});
      }, []);
      const refresh = useCallback(async () => {
        const ticket = ++listTicket.current;
        clearExplorer(); setCatalog(null); setError(''); setBusy('list');
        try {
          const value = await call('list', null);
          if (!value?.workflows || typeof value.workflows !== 'object' || Array.isArray(value.workflows)) throw new Error('The workflow catalog is invalid.');
          if (mounted.current && ticket === listTicket.current) setCatalog(value.workflows);
        } catch (failure) { if (mounted.current && ticket === listTicket.current) setError(failure.message || 'Unable to list workflows.'); }
        finally { if (mounted.current && ticket === listTicket.current) setBusy(''); }
      }, [call, clearExplorer]);
      useEffect(() => {
        mounted.current = true; closeRef.current?.focus();
        const unsubscribe = subscribeSaved(refresh);
        refresh();
        return () => { mounted.current = false; unsubscribe(); generation.current++; listTicket.current++; viewTicket.current++; settingsTicket.current++; };
      }, [refresh, subscribeSaved]);
      useEffect(() => { if (settings && !settingsBusy) settingsRef.current?.focus(); }, [!!settings, settingsBusy]);

      const inspect = async id => {
        if (busy || operation.current) return;
        clearExplorer(); const ticket = generation.current;
        setWorkflow(id); setError(''); setBusy('inspect'); operation.current = 'inspect';
        try {
          const result = await call('inspect', { workflowId: id });
          if (!mounted.current || ticket !== generation.current) return;
          if (!result?.artifacts || typeof result.artifacts !== 'object') throw new Error('The workflow artifact map is invalid.');
          setArtifacts(result.artifacts);
          if (result.artifacts.graph) {
            try {
              const file = await call('readArtifact', { workflowId: id, path: result.artifacts.graph });
              const model = graphModel(JSON.parse(file.content));
              if (mounted.current && ticket === generation.current) setGraph(model);
            } catch (failure) { if (mounted.current && ticket === generation.current) setGraphError(`Unable to load graph: ${failure.message || 'Invalid graph.'}`); }
          }
        } catch (failure) { if (mounted.current && ticket === generation.current) setError(failure.message || 'Unable to inspect workflow.'); }
        finally { operation.current = ''; if (mounted.current && ticket === generation.current) setBusy(''); }
        if (mounted.current && ticket === generation.current) loadFolder(id, '', ticket);
      };
      const loadFolder = async (id, path, generationId) => {
        const ticket = (folderTickets.current[path] || 0) + 1; folderTickets.current[path] = ticket;
        setFolders(old => ({ ...old, [path]: { loading: true } }));
        try {
          const result = await call('listFolder', { workflowId: id, path });
          if (!Array.isArray(result?.entries)) throw new Error('The folder listing is invalid.');
          if (mounted.current && generationId === generation.current && folderTickets.current[path] === ticket) setFolders(old => ({ ...old, [path]: { entries: result.entries, truncated: result.truncated } }));
        } catch (failure) { if (mounted.current && generationId === generation.current && folderTickets.current[path] === ticket) setFolders(old => ({ ...old, [path]: { error: failure.message || 'Unable to list folder.' } })); }
      };
      const toggleFolder = (path, retry = false) => {
        if (!workflow) return;
        if (folders[path] && !retry) {
          folderTickets.current[path] = (folderTickets.current[path] || 0) + 1;
          setFolders(old => { const next = { ...old }; for (const key of Object.keys(next)) if (key === path || key.startsWith(`${path}/`)) { delete next[key]; folderTickets.current[key] = (folderTickets.current[key] || 0) + 1; } return next; });
        } else loadFolder(workflow, path, generation.current);
      };
      const read = async (path, label, endpoint) => {
        if (!workflow || !path) return;
        const current = generation.current, ticket = ++viewTicket.current;
        setViewer({ path, label, loading: true });
        try {
          const file = await call(endpoint, { workflowId: workflow, path });
          if (typeof file?.content !== 'string') throw new Error('The file response is invalid.');
          if (mounted.current && current === generation.current && ticket === viewTicket.current) setViewer({ path, label, content: file.content, bytes: file.bytes });
        } catch (failure) { if (mounted.current && current === generation.current && ticket === viewTicket.current) setViewer({ path, label, error: failure.message || 'Unable to read file.' }); }
      };
      const openSettings = async () => {
        const ticket = ++settingsTicket.current;
        setSettings({ python: '', workspace: '', apiKey: '', apiKeySet: false }); setSettingsError(''); setSettingsBusy(true);
        try {
          const value = await call('getSettings', null);
          if (mounted.current && ticket === settingsTicket.current) setSettings({ python: value?.python || '', workspace: value?.workspace || '', apiKey: '', apiKeySet: !!value?.apiKeySet });
        } catch (failure) { if (mounted.current && ticket === settingsTicket.current) setSettingsError(failure.message || 'Unable to load settings.'); }
        finally { if (mounted.current && ticket === settingsTicket.current) { setSettingsBusy(false); settingsRef.current?.focus(); } }
      };
      const cancelSettings = () => { if (saving.current) return; settingsTicket.current++; setSettings(null); setSettingsError(''); setSettingsBusy(false); settingsButton.current?.focus(); };
      const saveSettings = async event => {
        event.preventDefault(); if (!settings || settingsBusy || saving.current || operation.current || busy) return;
        const ticket = ++settingsTicket.current; saving.current = true; setSettingsBusy(true); setSettingsError('');
        try {
          const { apiKeySet, ...payload } = settings;
          await call('saveSettings', payload);
          if (mounted.current && ticket === settingsTicket.current) { setSettings(null); settingsButton.current?.focus(); }
          notifySaved();
        } catch (failure) { if (mounted.current && ticket === settingsTicket.current) setSettingsError(failure.message || 'Unable to save settings.'); }
        finally { saving.current = false; if (mounted.current && ticket === settingsTicket.current) setSettingsBusy(false); }
      };
      const artifactButton = (label, path) => path && h('button', { type: 'button', key: label, style: styles.button, onClick: () => read(path, label, 'readArtifact') }, label);
      const node = graph?.nodes.find(item => item.name === selectedNode), nodeArtifacts = selectedNode && artifacts?.nodes?.[selectedNode];
      const workflows = catalog ? Object.entries(catalog).sort(([a], [b]) => compare(a, b)) : [];
      return h('aside', { id: 'flowx-overlay', role: 'region', 'aria-label': 'FlowX workflow explorer', style: styles.panel, onKeyDown: event => { if (event.key === 'Escape') { event.stopPropagation(); if (settings) cancelSettings(); else close(); } } },
        h('div', { style: styles.content },
          h('header', { style: styles.header }, h('div', null, h('p', { style: styles.eyebrow }, 'FlowX · read-only explorer'), h('h1', { style: styles.title }, 'Your workflows'), h('p', { style: styles.muted }, 'Explore plans and generated files without leaving your conversation.')),
            h('div', { style: styles.row }, h('button', { type: 'button', ref: settingsButton, style: styles.button, onClick: openSettings }, 'Settings'), h('button', { type: 'button', ref: closeRef, style: styles.button, onClick: close, 'aria-label': 'Close FlowX' }, 'Close'))),
          error && h('div', { role: 'alert', style: styles.notice }, error),
          h('section', { style: styles.card, 'aria-labelledby': 'flowx-workflows-heading' },
            h('div', { style: { ...styles.row, justifyContent: 'space-between', marginBottom: 10 } }, h('h2', { id: 'flowx-workflows-heading', style: styles.heading }, 'Workflows'), h('button', { type: 'button', style: styles.button, disabled: !!busy, onClick: refresh }, 'Refresh')),
            busy === 'list' ? h('p', { role: 'status', style: styles.muted }, 'Loading workflows…') : catalog && workflows.length === 0 ? h('p', { style: styles.muted }, 'No workflows found in this workspace.') :
              h('div', { style: { display: 'grid', gap: 6 } }, workflows.map(([id, info]) => h('button', { key: id, type: 'button', style: { ...styles.button, textAlign: 'left', background: workflow === id ? '#f0f3ff' : '#fff', overflowWrap: 'anywhere' }, disabled: !!busy, 'aria-pressed': workflow === id, onClick: () => inspect(id) }, id, h('span', { style: { ...styles.muted, display: 'block', fontWeight: 400 } }, `Published revision ${info?.workflow_version ?? '—'} · ${Object.keys(info?.nodes || {}).length} listed nodes`))))),
          workflow && h('section', { style: styles.card, 'aria-labelledby': 'flowx-explorer-heading' },
            h('p', { style: styles.eyebrow }, 'Workflow explorer'), h('h2', { id: 'flowx-explorer-heading', style: { ...styles.heading, overflowWrap: 'anywhere' } }, workflow),
            busy === 'inspect' && h('p', { role: 'status', style: styles.muted }, 'Loading workflow and graph…'),
            artifacts && h(React.Fragment, null,
              h('p', { style: { ...styles.muted, marginBottom: 10 } }, `Published workflow revision ${artifacts.workflow_version ?? '—'}`),
              h('div', { style: styles.row }, artifactButton('Requirement', artifacts.requirement), artifactButton('Main entrypoint', artifacts.main_entrypoint?.path), artifactButton('Graph JSON', artifacts.graph)),
              h('h3', { style: { ...styles.heading, marginTop: 16 } }, 'Current plan dependency graph'),
              h('p', { style: { ...styles.muted, marginBottom: 10 } }, 'Published artifacts may differ after graph-only updates. Select a node for details.'),
              graphError ? h('div', { role: 'alert', style: styles.notice }, graphError) : graph ? h(Graph, { model: graph, selected: selectedNode, onSelect: setSelectedNode }) : !artifacts.graph ? h('p', { style: styles.muted }, 'No graph artifact is available.') : null,
              node && h('div', { style: { marginTop: 12, padding: 12, border: '1px solid #e3e9f5', borderRadius: 10 } }, h('h3', { style: styles.heading }, node.name), h('p', { style: { ...styles.muted, whiteSpace: 'pre-wrap' } }, node.desc || 'No description provided.'), h('p', { style: { ...styles.muted, marginTop: 8 } }, `Status: ${node.enabled ? 'Enabled' : 'Disabled'} · Depends on: ${node.depends.length ? node.depends.join(', ') : 'None'}`), nodeArtifacts && h('p', { style: styles.muted }, `Node ID: ${nodeArtifacts.node_id} · Published node revision ${nodeArtifacts.version}`), h('div', { style: { ...styles.row, marginTop: 8 } }, nodeArtifacts && Object.entries(nodeArtifacts.artifacts || {}).sort(([a], [b]) => compare(a, b)).map(([label, path]) => artifactButton(label, path)))),
              h('h3', { style: { ...styles.heading, marginTop: 18 } }, 'Workflow folder'),
              h('p', { style: { ...styles.muted, marginBottom: 8 } }, 'Expand folders and open supported text files.'),
              h(Folder, { path: '', folders, onToggle: toggleFolder, onRead: path => read(path, 'File', 'readFolderFile') }),
              viewer && h('div', { style: { marginTop: 18 } }, h('h3', { style: styles.heading }, viewer.label), h('p', { style: { ...styles.muted, overflowWrap: 'anywhere' } }, viewer.path, viewer.bytes != null ? ` · ${viewer.bytes} bytes` : ''), viewer.loading ? h('p', { role: 'status', style: styles.muted }, 'Loading file…') : viewer.error ? h('div', { role: 'alert', style: styles.notice }, viewer.error) : h('pre', { style: styles.pre, tabIndex: 0 }, viewer.content)))),
          settings && h('div', { role: 'dialog', 'aria-modal': true, 'aria-labelledby': 'flowx-settings-title', onKeyDown: event => { if (event.key !== 'Tab') return; const controls = [...event.currentTarget.querySelectorAll('input:not(:disabled), button:not(:disabled)')]; if (!controls.length) { event.preventDefault(); return; } const first = controls[0], last = controls[controls.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }, style: { position: 'fixed', inset: 0, background: 'rgba(24, 39, 62, .45)', display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: 14, boxSizing: 'border-box', overflow: 'auto' } },
            h('form', { onSubmit: saveSettings, style: { ...styles.card, width: '100%', maxWidth: 520, boxShadow: '0 16px 50px rgba(0,0,0,.2)' } },
              h('h2', { id: 'flowx-settings-title', style: styles.heading }, 'FlowX settings'), h('p', { style: styles.muted }, 'Absolute paths on the Harness host. Saved values take precedence over environment variables. The API key is stored owner-only on the host and never shown again; leave it empty to keep the current one.'),
              settingsError && h('p', { role: 'alert', style: styles.notice }, settingsError),
              [['python', 'FLOWX_PYTHON'], ['workspace', 'FLOWX_WORKSPACE'], ['apiKey', 'FLOWX_LLM_API_KEY']].map(([key, label]) => h('div', { key }, h('label', { htmlFor: `flowx-${key}`, style: styles.label }, label), h('input', { id: `flowx-${key}`, ref: key === 'python' ? settingsRef : undefined, style: styles.input, type: key === 'apiKey' ? 'password' : 'text', autoComplete: key === 'apiKey' ? 'off' : undefined, placeholder: key === 'apiKey' && settings.apiKeySet ? 'Key is set' : undefined, value: settings[key], required: key !== 'apiKey', disabled: settingsBusy, onChange: event => setSettings(old => ({ ...old, [key]: event.target.value })) }))),
              h('div', { style: { ...styles.row, marginTop: 16 } }, h('button', { type: 'submit', style: styles.primary, disabled: settingsBusy || !!busy }, settingsBusy ? 'Working…' : 'Save settings'), h('button', { type: 'button', style: styles.button, disabled: saving.current, onClick: cancelSettings }, 'Cancel'))))));
    }

    return {
      inject: ['slots', 'layout'],
      apply(ctx) {
        let open = false, listeners = new Set(), savedListeners = new Set(), trigger = null;
        const publish = value => { open = value; for (const listener of listeners) listener(value); };
        const close = () => { publish(false); trigger?.focus(); };
        const subscribeSaved = listener => { savedListeners.add(listener); return () => savedListeners.delete(listener); };
        const notifySaved = () => { for (const listener of savedListeners) listener(); };
        function Launcher({ wide }) {
          const [expanded, setExpanded] = useState(open);
          useEffect(() => { listeners.add(setExpanded); return () => listeners.delete(setExpanded); }, []);
          return h('button', { type: 'button', title: 'FlowX workflows', 'aria-label': 'FlowX workflows', 'aria-expanded': expanded, 'aria-controls': 'flowx-overlay', onClick: event => { trigger = event.currentTarget; if (!open && trigger.closest('[role="dialog"][aria-modal="true"]')) { trigger = trigger.ownerDocument.querySelector('[data-sidebar-opener]') || trigger; ctx.layout.toggleSidebar(); } publish(!open); }, style: { display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px', border: 0, borderRadius: 8, background: expanded ? 'rgba(100, 115, 210, .16)' : 'transparent', color: 'inherit', cursor: 'pointer', font: 'inherit' } }, h(Icon), wide && h('span', null, 'FlowX'));
        }
        function Overlay() {
          const [expanded, setExpanded] = useState(open);
          useEffect(() => { listeners.add(setExpanded); return () => listeners.delete(setExpanded); }, []);
          return expanded ? h(Page, { call: callFlowX, close, subscribeSaved, notifySaved }) : null;
        }
        ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({ name: 'sidebar.footer.action', id: 'flowx', order: 100 }, Launcher));
        ctx.slots.inject('shell.overlay', () => ctx.slots.register({ name: 'shell.overlay', id: 'flowx', order: 100 }, Overlay));
      },
    };
  },
});