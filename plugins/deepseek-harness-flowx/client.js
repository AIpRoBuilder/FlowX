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
// Modified 2026-11-16: add HTML-over-SVG media canvas, per-node prompt/image/video previews, run selector, and layout persistence.
// Modified 2026-11-16: add a saved environment-variable area to plugin settings.

window.__ModuleLoader__.load({
  id: 'dsh-flowx-plugin',
  factory(require) {
    const React = require('react');
    const { createElement: h, useCallback, useEffect, useRef, useState } = React;
    const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
    const CARD_WIDTH = 236;
    const mediaHref = (workflowId, relPath) => `api/flowx/media?workflowId=${encodeURIComponent(workflowId)}&path=${encodeURIComponent(relPath)}`;
    const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
    const ENV_RESERVED = new Set(['FLOWX_PYTHON', 'FLOWX_WORKSPACE', 'FLOWX_LLM_API_KEY', 'FLOWX_PLUGIN_PYTHON_ACTIVE', '__PROTO__', 'CONSTRUCTOR', 'PROTOTYPE']);
    const parseEnvironment = text => {
      const set = {};
      for (const [index, raw] of String(text || '').split('\n').entries()) {
        let line = raw.trim();
        if (!line || line.startsWith('#')) continue;
        line = line.replace(/^export\s+/, '');
        const equals = line.indexOf('=');
        if (equals < 1) throw new Error(`Line ${index + 1}: expected KEY=VALUE.`);
        const name = line.slice(0, equals).trim();
        let value = line.slice(equals + 1).trim();
        if (!ENV_NAME.test(name)) throw new Error(`Line ${index + 1}: invalid variable name.`);
        if (ENV_RESERVED.has(name.toUpperCase())) throw new Error(`Line ${index + 1}: ${name} is managed by plugin settings.`);
        if (Object.hasOwn(set, name)) throw new Error(`Line ${index + 1}: duplicate ${name}.`);
        if (value.length >= 2 && (value.startsWith('"') && value.endsWith('"') || value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
        if (/[\x00-\x1f\x7f]/.test(value)) throw new Error(`Line ${index + 1}: values must be printable text.`);
        if (value.length > 4096) throw new Error(`Line ${index + 1}: value is too long.`);
        set[name] = value;
      }
      if (Object.keys(set).length > 64) throw new Error('At most 64 environment variables can be set at once.');
      return set;
    };
    const statusTone = status => status === 'running' ? '#3b82f6' : status === 'completed' ? '#16a34a' : status === 'failed' ? '#dc2626' : status === 'awaiting_input' ? '#d97706' : '#7181eb';
    const styles = {
      panel: { pointerEvents: 'auto', position: 'absolute', top: 8, bottom: 8, left: 8, width: 'min(680px, calc(100vw - 16px))', boxSizing: 'border-box', overflowY: 'auto', background: '#f5f8fc', color: '#192a44', border: '1px solid #dce5f3', borderRadius: 16, boxShadow: '0 20px 70px rgba(17, 33, 61, .25)', fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', fontSize: 14 },
      panelExpanded: { pointerEvents: 'auto', position: 'absolute', top: 8, bottom: 8, left: 8, width: 'calc(100vw - 16px)', boxSizing: 'border-box', overflowY: 'auto', background: '#f5f8fc', color: '#192a44', border: '1px solid #dce5f3', borderRadius: 16, boxShadow: '0 20px 70px rgba(17, 33, 61, .25)', fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif', fontSize: 14 },
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
      select: { border: '1px solid #cfd9e8', borderRadius: 8, padding: '6px 9px', color: '#192a44', font: 'inherit', fontSize: 12, background: '#fff' },
      badge: { display: 'inline-block', borderRadius: 999, padding: '2px 8px', fontSize: 11, fontWeight: 700, background: '#edf2ff', color: '#3b4cc0' },
      staleBadge: { display: 'inline-block', borderRadius: 999, padding: '2px 8px', fontSize: 11, fontWeight: 700, background: '#fff3cd', color: '#8a5a00', border: '1px solid #f5dca0' },
      label: { display: 'block', fontWeight: 700, fontSize: 12, margin: '14px 0 6px' },
      notice: { border: '1px solid #f0cbd0', color: '#953d4a', background: '#fff5f5', borderRadius: 9, padding: '10px 12px', fontSize: 13, overflowWrap: 'anywhere' },
      pre: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', overflow: 'auto', maxHeight: 380, background: '#15243a', color: '#e9f1ff', borderRadius: 9, padding: 12, fontSize: 12, lineHeight: 1.5 },
    };

    function graphModel(graph, layout = {}) {
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
      let maxX = 24, maxY = 24;
      for (const [layer, members] of layers) {
        members.forEach((name, row) => {
          const custom = layout && layout[name];
          const x = custom && Number.isFinite(custom.x) ? Math.max(12, custom.x) : 24 + layer * 296;
          const y = custom && Number.isFinite(custom.y) ? Math.max(12, custom.y) : 24 + row * 212;
          positions.set(name, { x, y });
          maxX = Math.max(maxX, x);
          maxY = Math.max(maxY, y);
        });
      }
      return {
        nodes: names.map(name => ({ ...byName.get(name), ...positions.get(name) })),
        edges,
        warnings,
        width: maxX + CARD_WIDTH + 48,
        height: maxY + 216,
      };
    }

    function Icon() {
      return h('svg', { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': true },
        h('path', { d: 'M5 6h6v6H5zM13 12h6v6h-6zM11 9h4v3M5 15h4', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }),
        h('circle', { cx: 5, cy: 15, r: 1.5, fill: 'currentColor' }));
    }

    function NodeCardPreview({ workflowId, nodeName, runNode, stale, onOpenLightbox }) {
      const card = runNode?.card;
      if (!card || typeof card !== 'object') return null;
      const kind = typeof card.kind === 'string' ? card.kind.toLowerCase() : '';
      const mediaPath = typeof card.media === 'string' && card.media ? card.media : '';
      const posterPath = typeof card.poster === 'string' && card.poster ? card.poster : '';
      const promptText = typeof card.prompt === 'string' && card.prompt ? card.prompt : typeof card.summary === 'string' ? card.summary : '';
      if (kind === 'image' && mediaPath) {
        const src = mediaHref(workflowId, mediaPath);
        const dims = Number.isFinite(card.width) && Number.isFinite(card.height) ? `${card.width}×${card.height}` : 'image';
        return h('div', { 'data-flowx-preview': 'image', style: { marginTop: 8, borderRadius: 8, overflow: 'hidden', border: '1px solid #e2e8f4', background: '#0f172a' } },
          h('img', { src, alt: card.label || `${nodeName} output`, loading: 'lazy', onClick: event => { event.stopPropagation(); onOpenLightbox?.({ kind: 'image', src, label: card.label || nodeName, dims }); }, style: { display: 'block', width: '100%', height: 112, objectFit: 'cover', cursor: 'zoom-in' } }),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 8px', background: '#f8faff', fontSize: 11, color: '#4c5d77' } },
            h('span', null, dims),
            stale && h('span', { style: styles.staleBadge }, 'stale')));
      }
      if (kind === 'video' && mediaPath) {
        const src = mediaHref(workflowId, mediaPath);
        const poster = posterPath ? mediaHref(workflowId, posterPath) : undefined;
        const dur = Number.isFinite(card.duration) ? `${Number(card.duration).toFixed(1)}s` : 'video';
        return h('div', { 'data-flowx-preview': 'video', onClick: event => event.stopPropagation(), style: { marginTop: 8, borderRadius: 8, overflow: 'hidden', border: '1px solid #e2e8f4', background: '#0f172a' } },
          h('video', { src, poster, controls: true, preload: 'metadata', style: { display: 'block', width: '100%', height: 116, objectFit: 'cover' } }),
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '4px 8px', background: '#f8faff', fontSize: 11, color: '#4c5d77' } },
            h('span', null, dur),
            h('div', { style: { display: 'flex', gap: 6, alignItems: 'center' } },
              stale && h('span', { style: styles.staleBadge }, 'stale'),
              h('button', { type: 'button', onClick: () => onOpenLightbox?.({ kind: 'video', src, poster, label: card.label || nodeName, dims: dur }), style: { border: 0, background: 'transparent', color: '#5366db', fontSize: 11, fontWeight: 700, cursor: 'pointer', padding: 0 } }, 'Expand'))));
      }
      if (promptText) {
        return h('div', { 'data-flowx-preview': kind || 'prompt', style: { marginTop: 8, padding: '6px 8px', borderRadius: 7, background: '#f3f6fc', border: '1px solid #e3eaf6', fontSize: 11, color: '#2b3e5e', lineHeight: 1.4, maxHeight: 62, overflow: 'hidden', overflowWrap: 'anywhere' } },
          promptText.length > 140 ? `${promptText.slice(0, 139)}…` : promptText,
          stale && h('div', { style: { marginTop: 4 } }, h('span', { style: styles.staleBadge }, 'stale')));
      }
      return null;
    }

    function Graph({ model, workflowId, run, workflowVersion, selected, zoom = 1, dragRef, onSelect, onMoveNode, onOpenLightbox }) {
      const positions = new Map(model.nodes.map(node => [node.name, node]));
      const staleRun = !!(run && Number.isInteger(workflowVersion) && workflowVersion > 0 && run.workflow_version > 0 && run.workflow_version !== workflowVersion);
      const startDrag = (event, node) => {
        if (!dragRef || !onMoveNode || event.button !== 0) return;
        dragRef.current = { name: node.name, startX: event.clientX, startY: event.clientY, origX: node.x, origY: node.y, moved: false };
      };
      const moveDrag = event => {
        const drag = dragRef?.current;
        if (!drag || !onMoveNode) return;
        const dx = (event.clientX - drag.startX) / (zoom || 1);
        const dy = (event.clientY - drag.startY) / (zoom || 1);
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;
        if (drag.moved) onMoveNode(drag.name, Math.max(12, Math.round(drag.origX + dx)), Math.max(12, Math.round(drag.origY + dy)), false);
      };
      const endDrag = event => {
        const drag = dragRef?.current;
        if (!drag) return;
        dragRef.current = null;
        if (drag.moved && onMoveNode) {
          const dx = (event.clientX - drag.startX) / (zoom || 1);
          const dy = (event.clientY - drag.startY) / (zoom || 1);
          onMoveNode(drag.name, Math.max(12, Math.round(drag.origX + dx)), Math.max(12, Math.round(drag.origY + dy)), true);
        }
      };
      return h('div', null,
        model.warnings.length > 0 && h('div', { role: 'status', style: { ...styles.notice, marginBottom: 10 } }, model.warnings.join(' ')),
        model.nodes.length === 0 ? h('p', { style: styles.muted }, 'This graph has no nodes.') :
          h('div', { onMouseMove: moveDrag, onMouseUp: endDrag, onMouseLeave: endDrag, style: { overflow: 'auto', maxHeight: 540, border: '1px solid #edf1f8', borderRadius: 12, background: 'radial-gradient(#dce4f2 1px, #f8faff 1px)', backgroundSize: '18px 18px' } },
            h('div', { style: { position: 'relative', width: Math.round(model.width * zoom), height: Math.round(model.height * zoom), minWidth: '100%' } },
              h('div', { style: { position: 'relative', width: model.width, height: model.height, transform: zoom === 1 ? undefined : `scale(${zoom})`, transformOrigin: '0 0' } },
                h('svg', { viewBox: `0 0 ${model.width} ${model.height}`, width: model.width, height: model.height, role: 'group', 'aria-label': 'Current plan dependency graph. Select a node to inspect it.', style: { display: 'block', maxWidth: 'none' } },
                  model.edges.map(edge => {
                    const from = positions.get(edge.from), to = positions.get(edge.to);
                    const x1 = from.x + CARD_WIDTH, y1 = from.y + 34, x2 = to.x, y2 = to.y + 34, bend = Math.max(36, Math.abs(x2 - x1) / 2);
                    return h('g', { key: `${edge.from}\0${edge.to}`, 'aria-hidden': true, 'data-flowx-edge': `${edge.from} → ${edge.to}` },
                      h('path', { d: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`, stroke: '#8ea0df', strokeWidth: 2.2, fill: 'none' }),
                      h('path', { d: `M ${x2 - 7} ${y2 - 4} L ${x2} ${y2} L ${x2 - 7} ${y2 + 4}`, stroke: '#8ea0df', strokeWidth: 2.2, fill: 'none' }));
                  }),
                  model.nodes.map(node => {
                    const runNode = run?.nodes?.[node.name];
                    const dotColor = !node.enabled ? '#8a6470' : runNode?.status ? statusTone(runNode.status) : '#7181eb';
                    return h('g', { key: node.name, role: 'button', tabIndex: 0, focusable: true, 'aria-label': `Inspect node ${node.name}${node.enabled ? '' : ' (disabled)'}`, 'aria-pressed': selected === node.name, 'data-flowx-node': node.name, onClick: () => onSelect(node.name), onKeyDown: event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(node.name); } }, style: { cursor: 'pointer' } },
                      h('title', null, `${node.name}${node.enabled ? '' : ' (disabled)'}`),
                      h('rect', { x: node.x, y: node.y, width: CARD_WIDTH, height: 68, rx: 12, fill: selected === node.name ? '#eef0ff' : '#fff', stroke: selected === node.name ? '#6071df' : '#dce4f2', strokeWidth: selected === node.name ? 2 : 1 }),
                      h('circle', { cx: node.x + 19, cy: node.y + 22, r: 5, fill: dotColor }),
                      h('text', { x: node.x + 32, y: node.y + 27, fill: '#223451', fontSize: 13, fontWeight: 700 }, node.name.length > 24 ? `${node.name.slice(0, 23)}…` : node.name),
                      h('text', { x: node.x + 19, y: node.y + 49, fill: '#5d6e87', fontSize: 11 }, `${node.depends.length} ${node.depends.length === 1 ? 'dependency' : 'dependencies'}${node.enabled ? '' : ' · Disabled'}${runNode?.status ? ` · ${runNode.status}` : ''}`));
                  })),
                model.nodes.map(node => {
                  const runNode = run?.nodes?.[node.name];
                  const hasCard = runNode?.card && typeof runNode.card === 'object' && Object.keys(runNode.card).length > 0;
                  if (!hasCard && !onMoveNode) return null;
                  return h('div', {
                    key: `card-${node.name}`,
                    'data-flowx-card': node.name,
                    onMouseDown: event => startDrag(event, node),
                    onClick: () => onSelect(node.name),
                    style: {
                      position: 'absolute',
                      left: node.x,
                      top: node.y,
                      width: CARD_WIDTH,
                      minHeight: 68,
                      boxSizing: 'border-box',
                      padding: hasCard ? '58px 10px 10px' : 0,
                      borderRadius: 12,
                      background: hasCard ? (selected === node.name ? 'rgba(238,240,255,.92)' : 'rgba(255,255,255,.95)') : 'transparent',
                      border: hasCard ? (selected === node.name ? '2px solid #6071df' : '1px solid #dce4f2') : '0',
                      boxShadow: hasCard ? '0 8px 20px rgba(25,42,68,.08)' : 'none',
                      pointerEvents: hasCard ? 'auto' : 'none',
                      cursor: 'pointer',
                    },
                  },
                  hasCard && h('div', { style: { position: 'absolute', top: 12, left: 14, right: 12, display: 'flex', alignItems: 'center', gap: 7, pointerEvents: 'none' } },
                    h('span', { style: { width: 9, height: 9, borderRadius: 99, background: !node.enabled ? '#8a6470' : statusTone(runNode?.status), flexShrink: 0 } }),
                    h('span', { style: { fontWeight: 700, fontSize: 13, color: '#223451', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, node.name),
                    runNode?.status && h('span', { style: { marginLeft: 'auto', fontSize: 10, fontWeight: 700, color: statusTone(runNode.status) } }, runNode.status)),
                  hasCard && h(NodeCardPreview, { workflowId, nodeName: node.name, runNode, stale: staleRun, onOpenLightbox }));
                })))));
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
      const [workflow, setWorkflow] = useState(null), [artifacts, setArtifacts] = useState(null), [rawGraph, setRawGraph] = useState(null), [layout, setLayout] = useState({}), [graphError, setGraphError] = useState('');
      const [runs, setRuns] = useState([]), [selectedRunId, setSelectedRunId] = useState(''), [activeRun, setActiveRun] = useState(null);
      const [wideCanvas, setWideCanvas] = useState(false), [zoom, setZoom] = useState(1), [lightbox, setLightbox] = useState(null);
      const [selectedNode, setSelectedNode] = useState(null), [viewer, setViewer] = useState(null), [folders, setFolders] = useState({});
      const [workflowEnv, setWorkflowEnv] = useState(null);
      const [settings, setSettings] = useState(null), [settingsError, setSettingsError] = useState(''), [settingsBusy, setSettingsBusy] = useState(false);
      const mounted = useRef(false), generation = useRef(0), listTicket = useRef(0), viewTicket = useRef(0), runTicket = useRef(0), folderTickets = useRef({}), envTicket = useRef(0), settingsTicket = useRef(0), operation = useRef(''), saving = useRef(false), dragRef = useRef(null);
      const closeRef = useRef(null), settingsRef = useRef(null), settingsButton = useRef(null);
      const clearExplorer = useCallback(() => {
        generation.current++; viewTicket.current++; runTicket.current++; envTicket.current++; folderTickets.current = {};
        setWorkflow(null); setArtifacts(null); setRawGraph(null); setLayout({}); setGraphError('');
        setRuns([]); setSelectedRunId(''); setActiveRun(null); setLightbox(null);
        setSelectedNode(null); setViewer(null); setFolders({}); setWorkflowEnv(null);
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
        return () => { mounted.current = false; unsubscribe(); generation.current++; listTicket.current++; viewTicket.current++; runTicket.current++; envTicket.current++; settingsTicket.current++; };
      }, [refresh, subscribeSaved]);
      useEffect(() => { if (settings && !settingsBusy) settingsRef.current?.focus(); }, [!!settings, settingsBusy]);

      const loadRunDetail = async (id, runId, generationId) => {
        if (!id || !runId) { setActiveRun(null); return; }
        const ticket = ++runTicket.current;
        try {
          const detail = await call('getRun', { workflowId: id, runId });
          if (mounted.current && generationId === generation.current && ticket === runTicket.current && detail?.run) {
            setActiveRun(detail.run);
          }
        } catch { /* optional run state */ }
      };
      const syncCanvas = async () => {
        if (!workflow || busy || operation.current) return;
        const id = workflow, generationId = generation.current;
        try {
          const savedLayout = await call('getLayout', { workflowId: id });
          if (mounted.current && generationId === generation.current && savedLayout?.positions && typeof savedLayout.positions === 'object') {
            setLayout(savedLayout.positions);
          }
        } catch { /* optional layout */ }
        try {
          const listing = await call('listRuns', { workflowId: id });
          if (!mounted.current || generationId !== generation.current || !Array.isArray(listing?.runs)) return;
          setRuns(listing.runs);
          const chosen = listing.latestRunId || listing.runs[0]?.run_id || '';
          setSelectedRunId(chosen);
          if (chosen) await loadRunDetail(id, chosen, generationId);
          else setActiveRun(null);
        } catch { /* optional runs */ }
      };
      const selectRun = async runId => {
        setSelectedRunId(runId);
        if (!workflow || !runId) { setActiveRun(null); return; }
        await loadRunDetail(workflow, runId, generation.current);
      };
      const moveNode = (name, x, y, persist) => {
        setLayout(old => {
          const next = { ...old, [name]: { x, y } };
          if (persist && workflow) call('saveLayout', { workflowId: workflow, positions: next }).catch(() => {});
          return next;
        });
      };
      const resetLayout = () => {
        setLayout({});
        if (workflow) call('saveLayout', { workflowId: workflow, positions: {} }).catch(() => {});
      };

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
              const parsed = JSON.parse(file.content);
              graphModel(parsed);
              if (mounted.current && ticket === generation.current) setRawGraph(parsed);
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
      const openWorkflowEnv = async () => {
        if (!workflow || busy || operation.current || workflowEnv?.busy) return;
        if (workflowEnv) { envTicket.current++; setWorkflowEnv(null); return; }
        const current = generation.current, ticket = ++envTicket.current;
        setWorkflowEnv({ loading: true, path: '', envKeys: [], draft: '', remove: [], notice: '', error: '' });
        try {
          const value = await call('getWorkflowEnvironment', { workflowId: workflow });
          if (!mounted.current || current !== generation.current || ticket !== envTicket.current) return;
          const envKeys = Array.isArray(value?.envKeys) ? value.envKeys.filter(key => typeof key === 'string') : [];
          setWorkflowEnv({ loading: false, path: typeof value?.path === 'string' ? value.path : '', envKeys, draft: '', remove: [], notice: '', error: '' });
        } catch (failure) {
          if (mounted.current && current === generation.current && ticket === envTicket.current) {
            setWorkflowEnv({ loading: false, path: '', envKeys: [], draft: '', remove: [], notice: '', error: failure.message || 'Unable to load the workflow environment.' });
          }
        }
      };
      const saveWorkflowEnv = async () => {
        if (!workflow || !workflowEnv || workflowEnv.busy || busy || operation.current) return;
        let set;
        try { set = parseEnvironment(workflowEnv.draft); }
        catch (failure) { setWorkflowEnv(old => old && ({ ...old, error: failure.message, notice: '' })); return; }
        const remove = workflowEnv.remove || [];
        if (Object.keys(set).length === 0 && remove.length === 0) {
          setWorkflowEnv(old => old && ({ ...old, error: 'Enter at least one KEY=VALUE line or remove a saved variable.', notice: '' }));
          return;
        }
        const current = generation.current, ticket = ++envTicket.current;
        setWorkflowEnv(old => old && ({ ...old, busy: true, error: '', notice: '' }));
        try {
          const value = await call('saveWorkflowEnvironment', { workflowId: workflow, set, remove });
          if (!mounted.current || current !== generation.current || ticket !== envTicket.current) return;
          const envKeys = Array.isArray(value?.envKeys) ? value.envKeys.filter(key => typeof key === 'string') : [];
          setWorkflowEnv(old => old && ({
            ...old, busy: false, path: typeof value?.path === 'string' ? value.path : old.path, envKeys,
            draft: '', remove: [], error: '',
            notice: `Saved ${envKeys.length} workflow environment ${envKeys.length === 1 ? 'variable' : 'variables'}. Restart this workflow's service to apply them.`,
          }));
        } catch (failure) {
          if (mounted.current && current === generation.current && ticket === envTicket.current) {
            setWorkflowEnv(old => old && ({ ...old, busy: false, error: failure.message || 'Unable to save the workflow environment.' }));
          }
        }
      };
      const openSettings = async () => {
        const ticket = ++settingsTicket.current;
        setSettings({ python: '', workspace: '', apiKey: '', apiKeySet: false, envKeys: [], envDraft: '', envRemove: [], envNotice: '', envError: '' }); setSettingsError(''); setSettingsBusy(true);
        try {
          const value = await call('getSettings', null);
          if (mounted.current && ticket === settingsTicket.current) {
            const envKeys = Array.isArray(value?.envKeys) ? value.envKeys.filter(key => typeof key === 'string') : [];
            setSettings({ python: value?.python || '', workspace: value?.workspace || '', apiKey: '', apiKeySet: !!value?.apiKeySet, envKeys, envDraft: '', envRemove: [], envNotice: '', envError: '' });
          }
        } catch (failure) { if (mounted.current && ticket === settingsTicket.current) setSettingsError(failure.message || 'Unable to load settings.'); }
        finally { if (mounted.current && ticket === settingsTicket.current) { setSettingsBusy(false); settingsRef.current?.focus(); } }
      };
      const cancelSettings = () => { if (saving.current) return; settingsTicket.current++; setSettings(null); setSettingsError(''); setSettingsBusy(false); settingsButton.current?.focus(); };
      const saveSettings = async event => {
        event.preventDefault(); if (!settings || settingsBusy || saving.current || operation.current || busy) return;
        const ticket = ++settingsTicket.current; saving.current = true; setSettingsBusy(true); setSettingsError('');
        try {
          await call('saveSettings', { python: settings.python, workspace: settings.workspace, apiKey: settings.apiKey });
          if (mounted.current && ticket === settingsTicket.current) { setSettings(null); settingsButton.current?.focus(); }
          notifySaved();
        } catch (failure) { if (mounted.current && ticket === settingsTicket.current) setSettingsError(failure.message || 'Unable to save settings.'); }
        finally { saving.current = false; if (mounted.current && ticket === settingsTicket.current) setSettingsBusy(false); }
      };
      const saveEnvironment = async () => {
        if (!settings || settingsBusy || saving.current || operation.current || busy) return;
        let set;
        try { set = parseEnvironment(settings.envDraft); }
        catch (failure) {
          if (mounted.current) setSettings(old => old && ({ ...old, envError: failure.message, envNotice: '' }));
          return;
        }
        const remove = settings.envRemove || [];
        if (Object.keys(set).length === 0 && remove.length === 0) {
          setSettings(old => old && ({ ...old, envError: 'Enter at least one KEY=VALUE line or remove a saved variable.', envNotice: '' }));
          return;
        }
        const ticket = ++settingsTicket.current; saving.current = true; setSettingsBusy(true);
        setSettings(old => old && ({ ...old, envError: '', envNotice: '' }));
        try {
          const value = await call('saveEnvironment', { set, remove });
          if (mounted.current && ticket === settingsTicket.current) {
            const envKeys = Array.isArray(value?.envKeys) ? value.envKeys.filter(key => typeof key === 'string') : [];
            setSettings(old => old && ({ ...old, envKeys, envDraft: '', envRemove: [], envNotice: `Saved ${envKeys.length} environment ${envKeys.length === 1 ? 'variable' : 'variables'}. Restart running FlowX processes to apply changes.`, envError: '' }));
          }
        } catch (failure) {
          if (mounted.current && ticket === settingsTicket.current) setSettings(old => old && ({ ...old, envError: failure.message || 'Unable to save environment variables.', envNotice: '' }));
        }
        finally { saving.current = false; if (mounted.current && ticket === settingsTicket.current) setSettingsBusy(false); }
      };
      const graph = rawGraph ? graphModel(rawGraph, layout) : null;
      const artifactButton = (label, path) => path && h('button', { type: 'button', key: label, style: styles.button, onClick: () => read(path, label, 'readArtifact') }, label);
      const node = graph?.nodes.find(item => item.name === selectedNode), nodeArtifacts = selectedNode && artifacts?.nodes?.[selectedNode];
      const selectedRunNode = selectedNode && activeRun?.nodes?.[selectedNode];
      const staleRun = !!(activeRun && Number.isInteger(artifacts?.workflow_version) && artifacts.workflow_version > 0 && activeRun.workflow_version > 0 && activeRun.workflow_version !== artifacts.workflow_version);
      const workflows = catalog ? Object.entries(catalog).sort(([a], [b]) => compare(a, b)) : [];
      const savedEnvKeys = settings?.envKeys || [], pendingEnvRemovals = settings?.envRemove || [];
      const keptEnvKeys = savedEnvKeys.filter(key => !pendingEnvRemovals.includes(key));
      return h('aside', { id: 'flowx-overlay', role: 'region', 'aria-label': 'FlowX workflow explorer', style: wideCanvas ? styles.panelExpanded : styles.panel, onKeyDown: event => { if (event.key === 'Escape') { event.stopPropagation(); if (lightbox) setLightbox(null); else if (settings) cancelSettings(); else close(); } } },
        h('div', { style: styles.content },
          h('header', { style: styles.header }, h('div', null, h('p', { style: styles.eyebrow }, 'FlowX · workflow & media canvas'), h('h1', { style: styles.title }, 'Your workflows'), h('p', { style: styles.muted }, 'Explore plans, generated files, and per-node media outputs without leaving your conversation.')),
            h('div', { style: styles.row },
              h('button', { type: 'button', style: styles.button, onClick: () => setWideCanvas(old => !old) }, wideCanvas ? 'Compact view' : 'Expand canvas'),
              h('button', { type: 'button', ref: settingsButton, style: styles.button, onClick: openSettings }, 'Settings'),
              h('button', { type: 'button', ref: closeRef, style: styles.button, onClick: close, 'aria-label': 'Close FlowX' }, 'Close'))),
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
              h('div', { style: styles.row },
                artifactButton('Requirement', artifacts.requirement), artifactButton('Main entrypoint', artifacts.main_entrypoint?.path), artifactButton('Graph JSON', artifacts.graph),
                h('button', { type: 'button', style: styles.button, 'aria-expanded': !!workflowEnv, disabled: !!busy, onClick: openWorkflowEnv }, 'Environment')),
              workflowEnv && h('div', { style: { marginTop: 12, padding: 12, border: '1px solid #e3e9f5', borderRadius: 10 } },
                h('h3', { style: styles.heading }, 'Service environment (.env)'),
                workflowEnv.loading ? h('p', { role: 'status', style: styles.muted }, 'Loading workflow environment…') :
                  h(React.Fragment, null,
                    h('p', { style: styles.muted }, `Saved to ${workflowEnv.path || 'the workflow service folder/.env'}. The generated service loads this file when it starts. Values are never shown here.`),
                    workflowEnv.error && h('p', { role: 'alert', style: styles.notice }, workflowEnv.error),
                    workflowEnv.envKeys.length > 0 && h('p', { style: styles.label }, 'Saved variables (values hidden)'),
                    workflowEnv.envKeys.length > 0 && h('div', { style: { ...styles.row, marginTop: 8 } },
                      workflowEnv.envKeys.filter(key => !(workflowEnv.remove || []).includes(key)).map(key => h('span', { key, style: { ...styles.badge, display: 'inline-flex', alignItems: 'center', gap: 6 } }, key,
                        h('button', { type: 'button', 'aria-label': `Remove workflow environment variable ${key}`, title: `Remove ${key}`, disabled: workflowEnv.busy, onClick: () => setWorkflowEnv(old => old && ({ ...old, remove: [...old.remove.filter(name => name !== key), key], error: '', notice: '' })), style: { border: 0, background: 'transparent', color: '#953d4a', cursor: 'pointer', fontWeight: 800, padding: '0 0 0 2px', font: 'inherit' } }, '×'))),
                      (workflowEnv.remove || []).length > 0 && h('button', { type: 'button', style: styles.button, disabled: workflowEnv.busy, onClick: () => setWorkflowEnv(old => old && ({ ...old, remove: [], error: '', notice: '' })) }, `Undo ${workflowEnv.remove.length} removal${workflowEnv.remove.length === 1 ? '' : 's'}`)),
                    h('label', { htmlFor: 'flowx-workflow-env', style: styles.label }, 'New or updated variables'),
                    h('textarea', { id: 'flowx-workflow-env', rows: 4, spellCheck: false, placeholder: 'OPENAI_BASE_URL=https://example.test/v1', value: workflowEnv.draft, disabled: workflowEnv.busy, onChange: event => setWorkflowEnv(old => old && ({ ...old, draft: event.target.value, error: '', notice: '' })), style: { ...styles.input, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', resize: 'vertical' } }),
                    workflowEnv.notice && h('p', { role: 'status', style: { ...styles.muted, color: '#166534', marginTop: 8 } }, workflowEnv.notice),
                    h('div', { style: { ...styles.row, marginTop: 10 } },
                      h('button', { type: 'button', style: styles.primary, disabled: workflowEnv.busy || ((workflowEnv.remove || []).length === 0 && workflowEnv.draft.trim() === ''), onClick: saveWorkflowEnv }, workflowEnv.busy ? 'Saving…' : 'Save service environment'),
                      h('button', { type: 'button', style: styles.button, disabled: workflowEnv.busy, onClick: () => setWorkflowEnv(old => old && ({ ...old, draft: '', remove: [], error: '', notice: '' })) }, 'Clear edits'),
                      h('button', { type: 'button', style: styles.button, disabled: workflowEnv.busy, onClick: () => setWorkflowEnv(null) }, 'Close environment')))),
              h('div', { style: { ...styles.row, justifyContent: 'space-between', marginTop: 16, marginBottom: 6 } },
                h('h3', { style: { ...styles.heading, margin: 0 } }, 'Current plan dependency graph'),
                graph && h('div', { style: styles.row },
                  runs.length > 0 && h('select', { 'aria-label': 'Select workflow run', value: selectedRunId, style: styles.select, onChange: event => selectRun(event.target.value) },
                    runs.map(item => h('option', { key: item.run_id, value: item.run_id }, `Run ${item.run_id.slice(0, 12)} · ${item.status} (rev ${item.workflow_version})`))),
                  activeRun && h('span', { style: staleRun ? styles.staleBadge : styles.badge }, staleRun ? `Run rev ${activeRun.workflow_version} · stale` : `Run · ${activeRun.status}`),
                  h('button', { type: 'button', style: styles.button, onClick: syncCanvas }, 'Sync outputs'),
                  Object.keys(layout).length > 0 && h('button', { type: 'button', style: styles.button, onClick: resetLayout }, 'Reset layout'),
                  h('button', { type: 'button', style: styles.button, onClick: () => setZoom(z => Math.max(0.6, Math.round((z - 0.15) * 100) / 100)) }, '−'),
                  h('span', { style: { fontSize: 12, color: '#5d6e87', minWidth: 38, textAlign: 'center' } }, `${Math.round(zoom * 100)}%`),
                  h('button', { type: 'button', style: styles.button, onClick: () => setZoom(z => Math.min(1.6, Math.round((z + 0.15) * 100) / 100)) }, '+'))),
              h('p', { style: { ...styles.muted, marginBottom: 10 } }, 'Published artifacts may differ after graph-only updates. Select a node for details, or drag cards with outputs to arrange layout.'),
              graphError ? h('div', { role: 'alert', style: styles.notice }, graphError) : graph ? h(Graph, { model: graph, workflowId: workflow, run: activeRun, workflowVersion: artifacts.workflow_version, selected: selectedNode, zoom, dragRef, onSelect: setSelectedNode, onMoveNode: moveNode, onOpenLightbox: setLightbox }) : !artifacts.graph ? h('p', { style: styles.muted }, 'No graph artifact is available.') : null,
              node && h('div', { style: { marginTop: 12, padding: 12, border: '1px solid #e3e9f5', borderRadius: 10 } },
                h('h3', { style: styles.heading }, node.name),
                h('p', { style: { ...styles.muted, whiteSpace: 'pre-wrap' } }, node.desc || 'No description provided.'),
                h('p', { style: { ...styles.muted, marginTop: 8 } }, `Status: ${node.enabled ? 'Enabled' : 'Disabled'} · Depends on: ${node.depends.length ? node.depends.join(', ') : 'None'}`),
                nodeArtifacts && h('p', { style: styles.muted }, `Node ID: ${nodeArtifacts.node_id} · Published node revision ${nodeArtifacts.version}`),
                selectedRunNode && h('div', { style: { marginTop: 8 } },
                  h('p', { style: { ...styles.muted, fontWeight: 700 } }, `Latest run status: ${selectedRunNode.status}${staleRun ? ' (stale revision)' : ''}`),
                  h(NodeCardPreview, { workflowId: workflow, nodeName: node.name, runNode: selectedRunNode, stale: staleRun, onOpenLightbox: setLightbox })),
                h('div', { style: { ...styles.row, marginTop: 8 } }, nodeArtifacts && Object.entries(nodeArtifacts.artifacts || {}).sort(([a], [b]) => compare(a, b)).map(([label, path]) => artifactButton(label, path)))),
              h('h3', { style: { ...styles.heading, marginTop: 18 } }, 'Workflow folder'),
              h('p', { style: { ...styles.muted, marginBottom: 8 } }, 'Expand folders and open supported text files.'),
              h(Folder, { path: '', folders, onToggle: toggleFolder, onRead: path => read(path, 'File', 'readFolderFile') }),
              viewer && h('div', { style: { marginTop: 18 } }, h('h3', { style: styles.heading }, viewer.label), h('p', { style: { ...styles.muted, overflowWrap: 'anywhere' } }, viewer.path, viewer.bytes != null ? ` · ${viewer.bytes} bytes` : ''), viewer.loading ? h('p', { role: 'status', style: styles.muted }, 'Loading file…') : viewer.error ? h('div', { role: 'alert', style: styles.notice }, viewer.error) : h('pre', { style: styles.pre, tabIndex: 0 }, viewer.content)))),
          lightbox && h('div', { role: 'dialog', 'aria-modal': true, 'aria-label': 'Media preview', onClick: () => setLightbox(null), style: { position: 'fixed', inset: 0, background: 'rgba(12, 20, 35, .76)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20, boxSizing: 'border-box', zIndex: 20 } },
            h('div', { onClick: event => event.stopPropagation(), style: { ...styles.card, maxWidth: 'min(920px, 92vw)', maxHeight: '90vh', overflow: 'auto', display: 'grid', gap: 10 } },
              h('div', { style: { ...styles.row, justifyContent: 'space-between' } },
                h('strong', null, lightbox.label, lightbox.dims ? ` · ${lightbox.dims}` : ''),
                h('button', { type: 'button', style: styles.button, onClick: () => setLightbox(null) }, 'Close preview')),
              lightbox.kind === 'video'
                ? h('video', { src: lightbox.src, poster: lightbox.poster, controls: true, autoPlay: true, style: { maxWidth: '100%', maxHeight: '74vh', borderRadius: 10, background: '#000' } })
                : h('img', { src: lightbox.src, alt: lightbox.label, style: { maxWidth: '100%', maxHeight: '74vh', borderRadius: 10, display: 'block' } }))),
          settings && h('div', { role: 'dialog', 'aria-modal': true, 'aria-labelledby': 'flowx-settings-title', onKeyDown: event => { if (event.key !== 'Tab') return; const controls = [...event.currentTarget.querySelectorAll('input:not(:disabled), textarea:not(:disabled), button:not(:disabled)')]; if (!controls.length) { event.preventDefault(); return; } const first = controls[0], last = controls[controls.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }, style: { position: 'fixed', inset: 0, background: 'rgba(24, 39, 62, .45)', display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: 14, boxSizing: 'border-box', overflow: 'auto' } },
            h('form', { onSubmit: saveSettings, style: { ...styles.card, width: '100%', maxWidth: 520, boxShadow: '0 16px 50px rgba(0,0,0,.2)' } },
              h('h2', { id: 'flowx-settings-title', style: styles.heading }, 'FlowX settings'), h('p', { style: styles.muted }, 'Absolute paths on the Harness host. Saved values take precedence over environment variables. The API key is stored owner-only on the host and never shown again; leave it empty to keep the current one.'),
              settingsError && h('p', { role: 'alert', style: styles.notice }, settingsError),
              [['python', 'FLOWX_PYTHON'], ['workspace', 'FLOWX_WORKSPACE'], ['apiKey', 'FLOWX_LLM_API_KEY']].map(([key, label]) => h('div', { key }, h('label', { htmlFor: `flowx-${key}`, style: styles.label }, label), h('input', { id: `flowx-${key}`, ref: key === 'python' ? settingsRef : undefined, style: styles.input, type: key === 'apiKey' ? 'password' : 'text', autoComplete: key === 'apiKey' ? 'off' : undefined, placeholder: key === 'apiKey' && settings.apiKeySet ? 'Key is set' : undefined, value: settings[key], required: key !== 'apiKey', disabled: settingsBusy, onChange: event => setSettings(old => ({ ...old, [key]: event.target.value })) }))),
              h('div', { style: { marginTop: 18, paddingTop: 14, borderTop: '1px solid #e3e9f5' } },
                h('h3', { style: styles.heading }, 'Environment variables'),
                h('p', { style: styles.muted }, 'Optional KEY=VALUE lines applied to FlowX build, inspection, and runtime processes that start after saving. Values are stored owner-only on the host and never shown again. Use the API key field above for FLOWX_LLM_API_KEY.'),
                savedEnvKeys.length > 0 && h('p', { style: styles.label }, 'Saved variables (values hidden)'),
                savedEnvKeys.length > 0 && h('div', { style: { ...styles.row, marginTop: 8 } },
                  keptEnvKeys.map(key => h('span', { key, style: { ...styles.badge, display: 'inline-flex', alignItems: 'center', gap: 6 } }, key,
                    h('button', { type: 'button', 'aria-label': `Remove environment variable ${key}`, title: `Remove ${key}`, disabled: settingsBusy, onClick: () => setSettings(old => old && ({ ...old, envRemove: [...old.envRemove.filter(name => name !== key), key], envError: '', envNotice: '' })), style: { border: 0, background: 'transparent', color: '#953d4a', cursor: 'pointer', fontWeight: 800, padding: '0 0 0 2px', font: 'inherit' } }, '×'))),
                  pendingEnvRemovals.length > 0 && h('button', { type: 'button', style: styles.button, disabled: settingsBusy, onClick: () => setSettings(old => old && ({ ...old, envRemove: [], envError: '', envNotice: '' })) }, `Undo ${pendingEnvRemovals.length} removal${pendingEnvRemovals.length === 1 ? '' : 's'}`)),
                h('label', { htmlFor: 'flowx-env', style: styles.label }, 'New or updated variables'),
                h('textarea', { id: 'flowx-env', rows: 4, spellCheck: false, placeholder: 'FLOWX_LLM_PROVIDER=deepseek\nFLOWX_MEDIA_DRY_RUN=1', value: settings.envDraft, disabled: settingsBusy, onChange: event => setSettings(old => old && ({ ...old, envDraft: event.target.value, envError: '', envNotice: '' })), style: { ...styles.input, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', resize: 'vertical' } }),
                settings.envError && h('p', { role: 'alert', style: styles.notice }, settings.envError),
                settings.envNotice && h('p', { role: 'status', style: { ...styles.muted, color: '#166534', marginTop: 8 } }, settings.envNotice),
                h('button', { type: 'button', style: { ...styles.primary, marginTop: 10 }, disabled: settingsBusy || (settings.envDraft.trim() === '' && pendingEnvRemovals.length === 0), onClick: saveEnvironment }, 'Set environment variables')),
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