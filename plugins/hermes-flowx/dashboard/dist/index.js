// SPDX-FileCopyrightText: 2026 FlowX contributors
// SPDX-License-Identifier: Apache-2.0
// FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
// Upstream: https://github.com/AIpRoBuilder/FlowX
// Added 2026-11-19: read-only FlowX workflow graph, run, and artifact viewer for the Hermes dashboard.

(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !SDK.React) {
    console.error("[flowx] Hermes plugin SDK is unavailable.");
    return;
  }

  var React = SDK.React;
  var hooks = SDK.hooks || React;
  var h = React.createElement;
  var useState = hooks.useState;
  var useEffect = hooks.useEffect;
  var useMemo = hooks.useMemo;
  var useRef = hooks.useRef;
  var useCallback = hooks.useCallback;

  var API = "/api/plugins/flowx";
  var NODE_W = 232;
  var NODE_H = 116;
  var TEXT_EXTENSIONS = {
    css: true, csv: true, html: true, ini: true, js: true, json: true, log: true,
    md: true, py: true, sql: true, toml: true, ts: true, txt: true, xml: true,
    yaml: true, yml: true,
  };
  var MEDIA_EXTENSIONS = {
    gif: "image/gif", jpeg: "image/jpeg", jpg: "image/jpeg", mp4: "video/mp4",
    png: "image/png", webm: "video/webm", webp: "image/webp",
  };

  function classNames() {
    return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
  }

  function encode(value) {
    return encodeURIComponent(value == null ? "" : String(value));
  }

  function workflowPath(workflowId, suffix) {
    return "/workflows/" + encode(workflowId) + (suffix || "");
  }

  function mediaURL(workflowId, relative) {
    return API + workflowPath(workflowId, "/media") + "?path=" + encode(relative);
  }

  function extensionOf(path) {
    var name = String(path || "").split("/").pop() || "";
    var index = name.lastIndexOf(".");
    return index >= 0 ? name.slice(index + 1).toLowerCase() : "";
  }

  function mimeFor(path) {
    return MEDIA_EXTENSIONS[extensionOf(path)] || "";
  }

  function readResponse(response) {
    return response.text().then(function (text) {
      var data = null;
      if (text) {
        try { data = JSON.parse(text); } catch (error) { data = { detail: text }; }
      }
      if (!response.ok) {
        var detail = data && data.detail;
        var message = typeof detail === "string"
          ? detail
          : (detail && detail.message) || response.statusText || ("HTTP " + response.status);
        throw new Error(message || ("HTTP " + response.status));
      }
      return data;
    });
  }

  function authedFetch(path, options) {
    if (typeof SDK.authedFetch === "function") {
      return SDK.authedFetch(API + path, options || {});
    }
    return fetch(API + path, Object.assign({ credentials: "same-origin" }, options || {}));
  }

  function getJSON(path) {
    if (typeof SDK.fetchJSON === "function") {
      return SDK.fetchJSON(API + path);
    }
    return authedFetch(path, { headers: { accept: "application/json" } }).then(readResponse);
  }

  function putJSON(path, body) {
    return authedFetch(path, {
      method: "PUT",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(readResponse);
  }

  function formatBytes(bytes) {
    if (typeof bytes !== "number" || !isFinite(bytes)) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KiB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MiB";
  }

  function formatTime(value) {
    if (!value) return "—";
    var date = new Date(value);
    return isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function timeAgo(value) {
    if (!value) return "";
    var date = new Date(value);
    if (isNaN(date.getTime())) return "";
    var seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return seconds + "s ago";
    if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
    if (seconds < 86400) return Math.round(seconds / 3600) + "h ago";
    return Math.round(seconds / 86400) + "d ago";
  }

  function statusClass(status) {
    return "status-" + String(status || "pending").replace(/[^A-Za-z0-9_-]/g, "-").toLowerCase();
  }

  function statusLabel(status) {
    var labels = {
      awaiting_input: "awaiting input",
      completed: "completed",
      failed: "failed",
      input_required: "input required",
      pending: "not run",
      running: "running",
      skipped: "skipped",
      cancelled: "cancelled",
    };
    return labels[status] || status || "unknown";
  }

  function layoutGraph(graph, saved) {
    var nodes = (graph && graph.nodes) || [];
    var byId = {};
    var index = {};
    nodes.forEach(function (node, position) {
      byId[node.id] = node;
      index[node.id] = position;
    });
    var depths = {};
    function depthOf(id, stack) {
      if (Object.prototype.hasOwnProperty.call(depths, id)) return depths[id];
      if (stack.indexOf(id) >= 0) return 0;
      var node = byId[id];
      if (!node) return 0;
      var deps = (node.dependencies || []).filter(function (dep) { return !!byId[dep]; });
      var depth = 0;
      if (deps.length) {
        depth = 1 + Math.max.apply(null, deps.map(function (dep) {
          return depthOf(dep, stack.concat(id));
        }));
      }
      depths[id] = depth;
      return depth;
    }
    var columns = {};
    nodes.forEach(function (node) {
      var depth = depthOf(node.id, []);
      if (!columns[depth]) columns[depth] = [];
      columns[depth].push(node);
    });
    var positions = {};
    Object.keys(columns).map(Number).sort(function (a, b) { return a - b; }).forEach(function (depth) {
      columns[depth].sort(function (a, b) {
        return String(a.name).localeCompare(String(b.name)) || index[a.id] - index[b.id];
      });
      columns[depth].forEach(function (node, row) {
        positions[node.id] = {
          x: 48 + depth * 298,
          y: 48 + row * 166,
        };
      });
    });
    if (saved) {
      Object.keys(saved).forEach(function (id) {
        if (positions[id] && saved[id] && isFinite(saved[id].x) && isFinite(saved[id].y)) {
          positions[id] = { x: Number(saved[id].x), y: Number(saved[id].y) };
        }
      });
    }
    var width = 860;
    var height = 520;
    Object.keys(positions).forEach(function (id) {
      width = Math.max(width, positions[id].x + NODE_W + 80);
      height = Math.max(height, positions[id].y + NODE_H + 80);
    });
    return { positions: positions, width: width, height: height };
  }

  function edgePath(from, to) {
    var x1 = from.x + NODE_W;
    var y1 = from.y + NODE_H / 2;
    var x2 = to.x;
    var y2 = to.y + NODE_H / 2;
    var bend = Math.max(56, Math.abs(x2 - x1) / 2);
    return "M " + x1 + " " + y1 + " C " + (x1 + bend) + " " + y1 + ", " +
      (x2 - bend) + " " + y2 + ", " + x2 + " " + y2;
  }

  function GraphView(props) {
    var workflow = props.workflow;
    var run = props.run;
    var saved = props.positions || {};
    var localState = useState(saved);
    var local = localState[0];
    var setLocal = localState[1];
    var scaleState = useState(1);
    var scale = scaleState[0];
    var setScale = scaleState[1];
    var panState = useState({ x: 0, y: 0 });
    var pan = panState[0];
    var setPan = panState[1];
    var panningState = useState(false);
    var panning = panningState[0];
    var setPanning = panningState[1];
    var dragRef = useRef(null);
    var panRef = useRef(null);
    var localRef = useRef(saved);
    var wrapRef = useRef(null);

    useEffect(function () {
      setLocal(saved || {});
      localRef.current = saved || {};
      setScale(1);
      setPan({ x: 0, y: 0 });
    }, [workflow.workflow_id]);

    useEffect(function () {
      if (!dragRef.current && !panRef.current) {
        setLocal(saved || {});
        localRef.current = saved || {};
      }
    }, [saved]);

    var layout = useMemo(function () {
      return layoutGraph(workflow.graph, local);
    }, [workflow.graph, local]);

    function updateNode(id, x, y) {
      var next = Object.assign({}, localRef.current);
      next[id] = { x: Math.round(x), y: Math.round(y) };
      localRef.current = next;
      setLocal(next);
    }

    function startNodeDrag(event, id) {
      if (event.button !== 0) return;
      event.stopPropagation();
      var position = layout.positions[id];
      if (!position) return;
      if (event.currentTarget.setPointerCapture) {
        try { event.currentTarget.setPointerCapture(event.pointerId); } catch (error) { /* ignore */ }
      }
      dragRef.current = {
        id: id,
        startX: event.clientX,
        startY: event.clientY,
        originX: position.x,
        originY: position.y,
        moved: false,
      };
      event.preventDefault();
    }

    function moveNode(event) {
      var drag = dragRef.current;
      if (!drag) return;
      var dx = (event.clientX - drag.startX) / scale;
      var dy = (event.clientY - drag.startY) / scale;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) drag.moved = true;
      updateNode(drag.id, drag.originX + dx, drag.originY + dy);
    }

    function endNodeDrag(event, id) {
      var drag = dragRef.current;
      dragRef.current = null;
      if (event.currentTarget.releasePointerCapture) {
        try { event.currentTarget.releasePointerCapture(event.pointerId); } catch (error) { /* ignore */ }
      }
      if (drag && drag.moved) {
        props.onSave(localRef.current);
      } else {
        props.onSelect(id);
      }
    }

    function startPan(event) {
      if (event.button !== 0) return;
      if (event.target && event.target.closest && event.target.closest(".flowx-node, button, input, select")) return;
      if (wrapRef.current && wrapRef.current.setPointerCapture) {
        try { wrapRef.current.setPointerCapture(event.pointerId); } catch (error) { /* ignore */ }
      }
      panRef.current = {
        startX: event.clientX,
        startY: event.clientY,
        originX: pan.x,
        originY: pan.y,
      };
      setPanning(true);
    }

    function movePan(event) {
      var state = panRef.current;
      if (!state) return;
      setPan({
        x: state.originX + (event.clientX - state.startX),
        y: state.originY + (event.clientY - state.startY),
      });
    }

    function endPan(event) {
      panRef.current = null;
      setPanning(false);
      if (wrapRef.current && wrapRef.current.releasePointerCapture) {
        try { wrapRef.current.releasePointerCapture(event.pointerId); } catch (error) { /* ignore */ }
      }
    }

    function zoom(delta) {
      setScale(function (current) {
        return Math.max(0.35, Math.min(2.2, current * delta));
      });
    }

    function fit() {
      var wrap = wrapRef.current;
      if (!wrap) return;
      var rect = wrap.getBoundingClientRect();
      var next = Math.max(0.35, Math.min(1.25, Math.min(
        (rect.width - 64) / layout.width,
        (rect.height - 64) / layout.height
      )));
      setScale(next);
      setPan({
        x: Math.max(20, (rect.width - layout.width * next) / 2),
        y: Math.max(20, (rect.height - layout.height * next) / 2),
      });
    }

    useEffect(function () {
      var wrap = wrapRef.current;
      if (!wrap || !wrap.addEventListener) return undefined;
      function handleWheel(event) {
        event.preventDefault();
        setScale(function (current) {
          return Math.max(0.35, Math.min(2.2, current * (event.deltaY < 0 ? 1.1 : 0.9)));
        });
      }
      wrap.addEventListener("wheel", handleWheel, { passive: false });
      return function () { wrap.removeEventListener("wheel", handleWheel); };
    }, []);

    function nodeStatus(node) {
      var entry = run && run.nodes && run.nodes[node.name];
      if (entry && entry.status) return entry.status;
      return node.enabled === false ? "pending" : "pending";
    }

    var edgeElements = (workflow.graph && workflow.graph.edges || []).map(function (edge, edgeIndex) {
      var from = layout.positions[edge.from];
      var to = layout.positions[edge.to];
      if (!from || !to) return null;
      var active = props.selectedId === edge.from || props.selectedId === edge.to;
      return h("path", {
        key: edge.from + "->" + edge.to + "-" + edgeIndex,
        d: edgePath(from, to),
        className: classNames("flowx-edge", active && "is-active"),
        markerEnd: "url(#flowx-arrow)",
      });
    });

    var nodeElements = (workflow.graph && workflow.graph.nodes || []).map(function (node) {
      var position = layout.positions[node.id];
      if (!position) return null;
      var status = nodeStatus(node);
      var entry = run && run.nodes && run.nodes[node.name];
      return h("div", {
        key: node.id,
        className: classNames(
          "flowx-node",
          statusClass(status),
          props.selectedId === node.id && "is-selected",
          node.enabled === false && "is-disabled"
        ),
        style: { left: position.x + "px", top: position.y + "px" },
        onPointerDown: function (event) { startNodeDrag(event, node.id); },
        onPointerMove: moveNode,
        onPointerUp: function (event) { endNodeDrag(event, node.id); },
        onPointerCancel: function () { dragRef.current = null; },
      },
        h("div", { className: "flowx-node-title" },
          h("span", null, node.name),
          h("span", {
            className: "flowx-status-dot " + statusClass(status),
            title: statusLabel(status),
          })
        ),
        h("div", { className: "flowx-node-desc" }, node.description || "No description."),
        h("div", { className: "flowx-node-foot" },
          h("span", null, node.type || node.meta_node_kind || "node"),
          h("span", null,
            (node.dependencies || []).length + " dep" + ((node.dependencies || []).length === 1 ? "" : "s")
            + (entry && entry.updated_at ? " · " + timeAgo(entry.updated_at) : "")
          )
        )
      );
    });

    return h("div", {
      ref: wrapRef,
      className: classNames("flowx-canvas-wrap", panning && "is-panning"),
      onPointerDown: startPan,
      onPointerMove: movePan,
      onPointerUp: endPan,
      onPointerLeave: endPan,
      onPointerCancel: endPan,
    },
      h("div", {
        className: "flowx-canvas",
        style: {
          width: layout.width + "px",
          height: layout.height + "px",
          transform: "translate(" + pan.x + "px, " + pan.y + "px) scale(" + scale + ")",
        },
      },
        h("svg", { className: "flowx-edges", width: layout.width, height: layout.height },
          h("defs", null,
            h("marker", {
              id: "flowx-arrow",
              markerWidth: "7",
              markerHeight: "7",
              refX: "6",
              refY: "3.5",
              orient: "auto",
            },
              h("path", { d: "M0,0 L7,3.5 L0,7 z", fill: "var(--flowx-accent, #5b9dff)" })
            )
          ),
          edgeElements
        ),
        nodeElements
      ),
      h("div", { className: "flowx-zoom" },
        h("button", {
          type: "button",
          className: "flowx-button",
          title: "Zoom out",
          onClick: function () { zoom(0.9); },
        }, "−"),
        h("span", { className: "flowx-zoom-label" }, Math.round(scale * 100) + "%"),
        h("button", {
          type: "button",
          className: "flowx-button",
          title: "Zoom in",
          onClick: function () { zoom(1.1); },
        }, "+"),
        h("button", {
          type: "button",
          className: "flowx-button",
          title: "Fit graph",
          onClick: fit,
        }, "Fit")
      )
    );
  }

  function Inspector(props) {
    var workflow = props.workflow;
    var run = props.run;
    var graph = workflow.graph || { nodes: [] };
    var node = null;
    (graph.nodes || []).forEach(function (item) {
      if (item.id === props.nodeId) node = item;
    });
    if (!node) {
      var latest = workflow.latest_run || (run ? { run_id: run.run_id, status: run.status } : null);
      return h("aside", { className: "flowx-inspector" },
        h("h3", null, workflow.workflow_id),
        h("p", null, "Select a node to inspect its plan metadata, artifacts, and run status."),
        h("h4", null, "Workflow"),
        h("div", { className: "flowx-kv" },
          h("span", null, "Version"), h("span", null, String(workflow.workflow_version || 0)),
          h("span", null, "Plan nodes"), h("span", null, String(workflow.node_count || 0)),
          h("span", null, "Built nodes"), h("span", null, String(workflow.built_node_count || 0)),
          h("span", null, "Main entry"), h("span", null, workflow.main_entrypoint_path || "not built"),
          h("span", null, "Updated"), h("span", null, formatTime(workflow.updated_at)),
          h("span", null, "Latest run"), h("span", null, latest ? latest.run_id + " · " + statusLabel(latest.status) : "none")
        ),
        (workflow.errors || []).length ? h("div", null,
          h("h4", null, "Unresolved errors"),
          h("div", { className: "flowx-chip-row" },
            workflow.errors.map(function (key) {
              return h("span", { className: "flowx-badge", key: key }, key);
            })
          )
        ) : null,
        workflow.requirement_path ? h("button", {
          type: "button",
          className: "flowx-button",
          style: { marginTop: "14px" },
          onClick: props.onRequirement,
        }, "View requirement analysis") : null,
        h("h4", null, "Legend"),
        h("div", { className: "flowx-chip-row" },
          ["completed", "running", "awaiting_input", "failed"].map(function (status) {
            return h("span", { className: "flowx-badge", key: status },
              h("span", { className: "flowx-status-dot " + statusClass(status) }),
              statusLabel(status)
            );
          })
        )
      );
    }
    var record = workflow.nodes && workflow.nodes[node.name] ? workflow.nodes[node.name] : null;
    var statusEntry = run && run.nodes && run.nodes[node.name];
    var dependents = (graph.nodes || []).filter(function (item) {
      return (item.dependencies || []).indexOf(node.name) >= 0;
    }).map(function (item) { return item.name; });
    return h("aside", { className: "flowx-inspector" },
      h("h3", null, node.name),
      h("div", { className: "flowx-chip-row", style: { marginTop: "6px" } },
        node.type ? h("span", { className: "flowx-badge flowx-accent", key: "type" }, node.type) : null,
        node.meta_node_kind ? h("span", { className: "flowx-badge", key: "kind" }, node.meta_node_kind) : null,
        h("span", { className: "flowx-badge", key: "status" },
          h("span", { className: "flowx-status-dot " + statusClass(statusEntry ? statusEntry.status : "pending") }),
          statusLabel(statusEntry ? statusEntry.status : "pending")
        ),
        node.enabled === false ? h("span", { className: "flowx-badge", key: "disabled" }, "disabled") : null
      ),
      h("h4", null, "Description"),
      h("p", null, node.description || "No description."),
      h("h4", null, "Graph"),
      h("div", { className: "flowx-kv" },
        h("span", null, "Node id"), h("span", null, node.id),
        h("span", null, "Depends on"), h("span", null, (node.dependencies || []).join(", ") || "none"),
        h("span", null, "Feeds"), h("span", null, dependents.join(", ") || "none"),
        h("span", null, "Loop"), h("span", null, node.loop == null ? "—" : String(node.loop)),
        h("span", null, "Artifact"), h("span", null, record ? (record.node_id || "—") + " · v" + record.version : "not built")
      ),
      node.ext_data && Object.keys(node.ext_data).length ? h("div", null,
        h("h4", null, "External data"),
        h("pre", { className: "flowx-pre" }, JSON.stringify(node.ext_data, null, 2))
      ) : null,
      record && record.artifacts && Object.keys(record.artifacts).length ? h("div", null,
        h("h4", null, "Artifacts"),
        Object.keys(record.artifacts).map(function (key) {
          return h("button", {
            type: "button",
            className: "flowx-artifact",
            key: key,
            onClick: function () { props.onPreview(record.artifacts[key]); },
          },
            h("span", null, key),
            h("code", null, record.artifacts[key])
          );
        })
      ) : null,
      statusEntry && statusEntry.card && Object.keys(statusEntry.card).length ? h("div", null,
        h("h4", null, "Latest card"),
        h("pre", { className: "flowx-pre" }, JSON.stringify(statusEntry.card, null, 2))
      ) : null
    );
  }

  function renderCard(card, workflowId, onPreview) {
    if (!card || typeof card !== "object") return null;
    var relative = typeof card.media === "string" ? card.media : "";
    if (card.kind === "image" && relative) {
      return h("img", {
        src: mediaURL(workflowId, relative),
        alt: card.label || relative,
        loading: "lazy",
        onClick: function () { onPreview(relative); },
      });
    }
    if (card.kind === "video" && relative) {
      return h("video", {
        controls: true,
        preload: "metadata",
        src: mediaURL(workflowId, relative),
        poster: card.poster ? mediaURL(workflowId, card.poster) : undefined,
      });
    }
    if (card.kind === "prompt" || card.prompt) {
      return h("div", { className: "flowx-step-prompt" }, String(card.prompt || ""));
    }
    if (Object.keys(card).length) {
      return h("pre", { className: "flowx-pre" }, JSON.stringify(card, null, 2));
    }
    return null;
  }

  function RunView(props) {
    var workflow = props.workflow;
    var runs = props.runsData || { runs: [] };
    var run = props.run;
    if (!runs.runs || !runs.runs.length) {
      return h("div", { className: "flowx-empty" },
        h("div", { className: "flowx-empty-card" },
          h("h3", null, "No recorded runs"),
          h("p", null, "Run the workflow's generated main entrypoint to create runs/<request_id>/run.json journals."),
          h("p", null, "The viewer reads those journals; it does not start or modify runs.")
        )
      );
    }
    var graphOrder = ((workflow.graph && workflow.graph.nodes) || []).map(function (node) { return node.name; });
    var runNodes = run && run.nodes ? run.nodes : {};
    var order = graphOrder.slice();
    Object.keys(runNodes).forEach(function (name) {
      if (order.indexOf(name) < 0) order.push(name);
    });
    return h("div", { className: "flowx-runs" },
      h("div", { className: "flowx-run-toolbar" },
        h("select", {
          className: "flowx-select",
          value: props.selectedRunId || "",
          onChange: function (event) { props.onSelectRun(event.target.value); },
        },
          runs.runs.map(function (item) {
            return h("option", { value: item.run_id, key: item.run_id },
              item.run_id + " · " + statusLabel(item.status) + " · " + timeAgo(item.updated_at || item.created_at)
            );
          })
        ),
        h("button", {
          type: "button",
          className: "flowx-button",
          onClick: props.onRefresh,
        }, "Refresh")
      ),
      h("div", { className: "flowx-run-body" },
        run ? h("div", { className: "flowx-run-summary" },
          h("div", { className: "flowx-metric" },
            h("div", { className: "flowx-metric-label" }, "Status"),
            h("div", { className: "flowx-metric-value" }, statusLabel(run.status))
          ),
          h("div", { className: "flowx-metric" },
            h("div", { className: "flowx-metric-label" }, "Run"),
            h("div", { className: "flowx-metric-value" }, run.run_id)
          ),
          h("div", { className: "flowx-metric" },
            h("div", { className: "flowx-metric-label" }, "Workflow version"),
            h("div", { className: "flowx-metric-value" }, String(run.workflow_version || 0))
          ),
          h("div", { className: "flowx-metric" },
            h("div", { className: "flowx-metric-label" }, "Updated"),
            h("div", { className: "flowx-metric-value" }, formatTime(run.updated_at || run.created_at))
          )
        ) : null,
        run && run.has_error ? h("div", { className: "flowx-error", style: { marginBottom: "10px" } },
          "This run recorded an error. The viewer withholds error details."
        ) : null,
        run && run.awaiting ? h("div", { className: "flowx-badge flowx-accent", style: { marginBottom: "10px" } },
          "Awaiting input at " + (run.awaiting.stepId || run.awaiting.step_id || "a step")
        ) : null,
        run ? h("div", { className: "flowx-steps" },
          order.map(function (name) {
            var entry = runNodes[name] || {};
            return h("div", { className: "flowx-step " + statusClass(entry.status || "pending"), key: name },
              h("div", { className: "flowx-step-head" },
                h("div", { className: "flowx-step-title" }, entry.title || name),
                h("span", { className: "flowx-badge" },
                  h("span", { className: "flowx-status-dot " + statusClass(entry.status || "pending") }),
                  statusLabel(entry.status || "pending")
                )
              ),
              h("div", { className: "flowx-step-body" },
                entry.updated_at ? h("div", { className: "flowx-badge", style: { marginBottom: "7px" } }, timeAgo(entry.updated_at)) : null,
                renderCard(entry.card, workflow.workflow_id, props.onPreview) || h("p", null, "No card data.")
              )
            );
          })
        ) : h("div", { className: "flowx-empty" }, "Select a run.")
      )
    );
  }

  function FilesView(props) {
    var entries = props.files ? props.files.entries || [] : [];
    var path = props.files ? props.files.path || "" : "";
    var parts = path ? path.split("/") : [];
    var crumbs = [h("button", {
      type: "button",
      className: "flowx-crumb",
      key: "root",
      onClick: function () { props.onNavigate(""); },
    }, "workflow")];
    var accumulated = "";
    parts.forEach(function (part) {
      accumulated = accumulated ? accumulated + "/" + part : part;
      var target = accumulated;
      crumbs.push(h("span", { key: "sep-" + target }, "/"));
      crumbs.push(h("button", {
        type: "button",
        className: "flowx-crumb",
        key: "crumb-" + target,
        onClick: function () { props.onNavigate(target); },
      }, part));
    });
    return h("div", { className: "flowx-files" },
      h("div", { className: "flowx-breadcrumb" }, crumbs),
      h("div", { className: "flowx-file-list" },
        entries.length ? entries.map(function (entry) {
          var isDir = entry.kind === "dir";
          var isMedia = entry.kind === "media";
          var isText = entry.kind === "text";
          var disabled = !entry.readable || (!isDir && !isMedia && !isText);
          return h("button", {
            type: "button",
            className: "flowx-file-row",
            key: entry.path,
            disabled: disabled,
            onClick: function () {
              if (isDir) props.onNavigate(entry.path);
              else props.onPreview(entry.path);
            },
          },
            h("span", { className: "flowx-file-icon" }, isDir ? "▸" : isMedia ? "▣" : isText ? "≡" : "·"),
            h("span", { className: "flowx-file-name" }, entry.name),
            h("span", { className: "flowx-file-kind" }, entry.kind),
            h("span", { className: "flowx-file-size" }, isDir ? "" : formatBytes(entry.size))
          );
        }) : h("div", { className: "flowx-empty" }, "This folder is empty.")
      )
    );
  }

  function FlowXPage() {
    var workflowsState = useState([]);
    var workflows = workflowsState[0];
    var setWorkflows = workflowsState[1];
    var settingsState = useState({});
    var settings = settingsState[0];
    var setSettings = settingsState[1];
    var selectedState = useState("");
    var selected = selectedState[0];
    var setSelected = selectedState[1];
    var detailState = useState(null);
    var detail = detailState[0];
    var setDetail = detailState[1];
    var runsState = useState({ runs: [], latestRunId: null });
    var runsData = runsState[0];
    var setRunsData = runsState[1];
    var runState = useState(null);
    var run = runState[0];
    var setRun = runState[1];
    var selectedRunState = useState("");
    var selectedRunId = selectedRunState[0];
    var setSelectedRunId = selectedRunState[1];
    var tabState = useState("plan");
    var tab = tabState[0];
    var setTab = tabState[1];
    var loadingState = useState(true);
    var loading = loadingState[0];
    var setLoading = loadingState[1];
    var errorState = useState("");
    var error = errorState[0];
    var setError = errorState[1];
    var filterState = useState("");
    var filter = filterState[0];
    var setFilter = filterState[1];
    var selectedNodeState = useState("");
    var selectedNode = selectedNodeState[0];
    var setSelectedNode = selectedNodeState[1];
    var positionsState = useState({});
    var positions = positionsState[0];
    var setPositions = positionsState[1];
    var filesState = useState(null);
    var files = filesState[0];
    var setFiles = filesState[1];
    var filePathState = useState("");
    var filePath = filePathState[0];
    var setFilePath = filePathState[1];
    var previewState = useState(null);
    var preview = previewState[0];
    var setPreview = previewState[1];
    var refreshState = useState(0);
    var refreshToken = refreshState[0];
    var setRefreshToken = refreshState[1];
    var detailSeq = useRef(0);
    var runSeq = useRef(0);
    var fileSeq = useRef(0);
    var selectedRef = useRef(selected);
    selectedRef.current = selected;

    var loadWorkflows = useCallback(function () {
      setLoading(true);
      setError("");
      getJSON("/workflows").then(function (data) {
        var list = data.workflows || [];
        setSettings(data);
        setWorkflows(list);
        setSelected(function (current) {
          if (current && list.some(function (item) { return item.workflow_id === current; })) return current;
          return list.length ? list[0].workflow_id : "";
        });
      }).catch(function (failure) {
        setError(failure.message || "Unable to load FlowX workflows.");
      }).then(function () {
        setLoading(false);
      });
    }, []);

    useEffect(function () {
      loadWorkflows();
    }, [loadWorkflows, refreshToken]);

    var loadRun = useCallback(function (workflowId, runId) {
      if (!runId) {
        setRun(null);
        setSelectedRunId("");
        return;
      }
      var seq = ++runSeq.current;
      getJSON(workflowPath(workflowId, "/runs/" + encode(runId))).then(function (data) {
        if (seq !== runSeq.current || workflowId !== selectedRef.current) return;
        setRun(data.run || null);
        setSelectedRunId(runId);
      }).catch(function (failure) {
        if (seq !== runSeq.current || workflowId !== selectedRef.current) return;
        setRun(null);
        setSelectedRunId("");
        setError(failure.message || "Unable to load the selected run.");
      });
    }, []);

    useEffect(function () {
      if (!selected) {
        setDetail(null);
        setRunsData({ runs: [], latestRunId: null });
        setRun(null);
        return;
      }
      var seq = ++detailSeq.current;
      setDetail(null);
      setRun(null);
      setSelectedNode("");
      setFiles(null);
      setFilePath("");
      Promise.all([
        getJSON(workflowPath(selected)),
        getJSON(workflowPath(selected, "/runs")),
        getJSON(workflowPath(selected, "/layout")),
      ]).then(function (results) {
        if (seq !== detailSeq.current) return;
        setDetail(results[0].workflow || null);
        setRunsData(results[1] || { runs: [], latestRunId: null });
        setPositions(results[2].positions || {});
        var latest = results[1].latestRunId || ((results[1].runs || [])[0] || {}).run_id || "";
        if (latest) loadRun(selected, latest);
      }).catch(function (failure) {
        if (seq !== detailSeq.current) return;
        setError(failure.message || "Unable to load the workflow.");
      });
    }, [selected, refreshToken, loadRun]);

    useEffect(function () {
      if (tab !== "files" || !detail) return;
      var seq = ++fileSeq.current;
      getJSON(workflowPath(detail.workflow_id, "/files?path=" + encode(filePath))).then(function (data) {
        if (seq !== fileSeq.current || detail.workflow_id !== selectedRef.current) return;
        setFiles(data);
      }).catch(function (failure) {
        if (seq !== fileSeq.current || detail.workflow_id !== selectedRef.current) return;
        setFiles({ path: filePath, entries: [] });
        setError(failure.message || "Unable to list the workflow folder.");
      });
    }, [tab, detail, filePath]);

    function saveLayout(nextPositions) {
      if (!selected) return;
      var workflowId = selected;
      putJSON(workflowPath(workflowId, "/layout"), { positions: nextPositions }).then(function (data) {
        if (workflowId === selectedRef.current && data && data.positions) setPositions(data.positions);
      }).catch(function (failure) {
        console.warn("[flowx] layout save failed:", failure.message);
      });
    }

    function openPreview(path) {
      if (!selected) return;
      var workflowId = selected;
      var extension = extensionOf(path);
      if (MEDIA_EXTENSIONS[extension]) {
        setPreview({ kind: "media", path: path, mime: MEDIA_EXTENSIONS[extension] });
        return;
      }
      getJSON(workflowPath(workflowId, "/file?path=" + encode(path))).then(function (data) {
        if (workflowId !== selectedRef.current) return;
        setPreview({ kind: "text", path: data.path || path, content: data.content || "", bytes: data.bytes });
      }).catch(function (failure) {
        if (workflowId !== selectedRef.current) return;
        setError(failure.message || "Unable to read the file.");
      });
    }

    function openRequirement() {
      if (!selected) return;
      var workflowId = selected;
      getJSON(workflowPath(workflowId, "/requirement")).then(function (data) {
        if (workflowId !== selectedRef.current) return;
        setPreview({ kind: "text", path: data.path || "plan/requirement_analysis.md", content: data.content || "", bytes: data.bytes });
      }).catch(function (failure) {
        if (workflowId !== selectedRef.current) return;
        setError(failure.message || "Unable to read the requirement analysis.");
      });
    }

    function selectWorkflow(workflowId) {
      setSelected(workflowId);
      setTab("plan");
      setPreview(null);
    }

    var filtered = workflows.filter(function (workflow) {
      if (!filter.trim()) return true;
      return workflow.workflow_id.toLowerCase().indexOf(filter.trim().toLowerCase()) >= 0;
    });

    function renderSetup() {
      return h("div", { className: "flowx-empty" },
        h("div", { className: "flowx-empty-card" },
          h("h3", null, settings.workspace_exists ? "No workflows found" : "Connect a FlowX workspace"),
          h("p", null, settings.workspace_exists
            ? "This workspace exists but has no workflows/<workflow_id> folders yet. Create or build a workflow, then refresh."
            : "The viewer reads workflow plans, run journals, and artifacts from a FlowX workspace. It never builds or mutates a workflow."),
          h("p", null, settings.workspace_exists
            ? "To use a different workspace, set the Hermes plugin setting or the gateway environment:"
            : "Set the workspace in the Hermes plugin settings or in the gateway environment:"),
          h("code", { className: "flowx-code" },
            "hermes config set plugins.entries.flowx.settings.workspace /absolute/path/to/flowx_workspaces\n" +
            "# or\nexport FLOWX_WORKSPACE=/absolute/path/to/flowx_workspaces"
          ),
          settings.workspace ? h("p", null, "Current path: ", h("code", null, settings.workspace)) : null,
          h("button", {
            type: "button",
            className: "flowx-button",
            style: { marginTop: "12px" },
            onClick: function () { setRefreshToken(function (value) { return value + 1; }); },
          }, "Retry")
        )
      );
    }

    function renderPreview() {
      if (!preview) return null;
      var body;
      if (preview.kind === "text") {
        body = h("pre", { className: "flowx-pre" }, preview.content || "");
      } else if (preview.mime && preview.mime.indexOf("video/") === 0) {
        body = h("video", { controls: true, src: mediaURL(selected, preview.path) });
      } else {
        body = h("img", { src: mediaURL(selected, preview.path), alt: preview.path });
      }
      return h("div", {
        className: "flowx-modal-backdrop",
        onClick: function () { setPreview(null); },
      },
        h("div", { className: "flowx-modal", onClick: function (event) { event.stopPropagation(); } },
          h("div", { className: "flowx-modal-head" },
            h("div", null,
              h("div", { className: "flowx-modal-title" }, preview.path),
              h("div", { className: "flowx-modal-path" },
                preview.kind === "text" ? formatBytes(preview.bytes) : (preview.mime || "")
              )
            ),
            h("button", {
              type: "button",
              className: "flowx-button",
              onClick: function () { setPreview(null); },
            }, "Close")
          ),
          h("div", { className: "flowx-modal-body" }, body)
        )
      );
    }

    var header = h("div", { className: "flowx-header" },
      h("div", { className: "flowx-brand" },
        h("div", { className: "flowx-logo" }, "FX"),
        h("div", { style: { minWidth: 0 } },
          h("h1", { className: "flowx-title" }, "FlowX"),
          h("p", { className: "flowx-subtitle" },
            settings.workspace
              ? settings.workspace + (settings.workspace_exists ? "" : " · workspace not found")
              : "Read-only workflow viewer"
          )
        )
      ),
      h("div", { className: "flowx-header-actions" },
        h("span", { className: "flowx-badge" }, "read-only"),
        h("span", { className: "flowx-badge" }, workflows.length + " workflow" + (workflows.length === 1 ? "" : "s")),
        h("button", {
          type: "button",
          className: "flowx-button",
          disabled: loading,
          onClick: function () { setRefreshToken(function (value) { return value + 1; }); },
        }, loading ? "Loading…" : "Refresh")
      )
    );

    if (loading && !workflows.length) {
      return h("div", { className: "flowx-root" }, header,
        h("div", { className: "flowx-body" },
          h("div", { className: "flowx-empty" }, "Loading FlowX workspace…")
        )
      );
    }

    if (!settings.workspace_exists || (!workflows.length && !detail)) {
      return h("div", { className: "flowx-root" }, header,
        error ? h("div", { className: "flowx-error" }, error) : null,
        h("div", { className: "flowx-body" }, renderSetup()),
        renderPreview()
      );
    }

    var sidebar = h("div", { className: "flowx-sidebar" },
      h("div", { className: "flowx-sidebar-head" },
        h("input", {
          className: "flowx-search",
          value: filter,
          placeholder: "Filter workflows…",
          onChange: function (event) { setFilter(event.target.value); },
        }),
        h("div", { className: "flowx-sidebar-meta" },
          h("span", null, filtered.length + " shown"),
          h("span", null, settings.single_workflow ? "single workflow" : "workspace")
        )
      ),
      h("div", { className: "flowx-workflow-list" },
        filtered.map(function (workflow) {
          var latest = workflow.latest_run;
          return h("button", {
            type: "button",
            key: workflow.workflow_id,
            className: classNames("flowx-workflow", selected === workflow.workflow_id && "is-selected"),
            onClick: function () { selectWorkflow(workflow.workflow_id); },
          },
            h("div", { className: "flowx-workflow-name" },
              h("span", { className: "flowx-status-dot " + statusClass(latest ? latest.status : "pending") }),
              h("span", null, workflow.workflow_id)
            ),
            h("div", { className: "flowx-workflow-meta" },
              h("span", null, "v" + (workflow.workflow_version || 0)),
              h("span", null, (workflow.node_count || 0) + " nodes"),
              workflow.has_main ? h("span", null, "main built") : h("span", null, "no main"),
              latest ? h("span", null, statusLabel(latest.status)) : null,
              workflow.updated_at ? h("span", null, timeAgo(workflow.updated_at)) : null
            )
          );
        }),
        filtered.length ? null : h("div", { className: "flowx-empty" }, "No workflows match this filter.")
      )
    );

    var main = h("div", { className: "flowx-main" },
      h("div", { className: "flowx-main-head" },
        h("div", { className: "flowx-tabs" },
          ["plan", "runs", "files"].map(function (name) {
            return h("button", {
              type: "button",
              key: name,
              className: classNames("flowx-tab", tab === name && "is-active"),
              onClick: function () { setTab(name); },
            }, name.charAt(0).toUpperCase() + name.slice(1));
          })
        ),
        h("div", { className: "flowx-main-meta" },
          detail ? h("span", { className: "flowx-badge" }, "v" + (detail.workflow_version || 0)) : null,
          detail ? h("span", { className: "flowx-badge" }, (detail.node_count || 0) + " nodes") : null,
          run ? h("span", { className: "flowx-badge flowx-accent" }, statusLabel(run.status)) : null
        )
      ),
      !detail ? h("div", { className: "flowx-empty" }, "Select a workflow to inspect.")
        : tab === "plan" ? h("div", { className: "flowx-content" },
          h(GraphView, {
            workflow: detail,
            run: run,
            positions: positions,
            onSave: saveLayout,
            selectedId: selectedNode,
            onSelect: setSelectedNode,
          }),
          h(Inspector, {
            workflow: detail,
            run: run,
            nodeId: selectedNode,
            onPreview: openPreview,
            onRequirement: openRequirement,
          })
        )
        : tab === "runs" ? h("div", { className: "flowx-content" },
          h(RunView, {
            workflow: detail,
            runsData: runsData,
            run: run,
            selectedRunId: selectedRunId,
            onSelectRun: function (runId) { loadRun(detail.workflow_id, runId); },
            onRefresh: function () { setRefreshToken(function (value) { return value + 1; }); },
            onPreview: openPreview,
          })
        )
        : h("div", { className: "flowx-content" },
          h(FilesView, {
            files: files,
            onNavigate: function (path) { setFilePath(path); },
            onPreview: openPreview,
          })
        )
    );

    return h("div", { className: "flowx-root" },
      header,
      error ? h("div", { className: "flowx-error" }, error) : null,
      h("div", { className: "flowx-body" }, sidebar, main),
      renderPreview()
    );
  }

  if (!window.__HERMES_PLUGINS__ || typeof window.__HERMES_PLUGINS__.register !== "function") {
    console.error("[flowx] Hermes plugin registry is unavailable.");
    return;
  }
  window.__HERMES_PLUGINS__.register("flowx", FlowXPage);
})();
