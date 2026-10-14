# AG-UI Lifecycle Main Backend Prompt

Modification note (2026-10-13): use explicit JSON node-class maps and decode HTTP file inputs to bytes for the updated runtime.
Modification note (2026-10-14): define concrete classes in a NODES tuple and pass its NODE_CLASSES map unchanged.

Generate a runnable FastAPI backend using `WorkflowEngine` and `WorkflowPipelinePool` from `ag_ui_workflow`, not a `GPipeline` CLI. Follow the main entrypoint template.

## Runtime ownership

- Load `.env` from the generated file directory and project root before creating runtime resources, with `load_dotenv(..., override=False)`.
- Import generated node classes from the absolute root package, so script execution works. Do not use relative node imports, `.step_nodes`, import fallbacks, or dynamic module discovery.
- Define `NODES = (NodeA, NodeB, ...)`, `NODE_CLASSES = {node.STEP_ID: node for node in NODES}`, and `STEP_CHAIN = [NodeA.step_meta(), ...]`. Preserve a trailing comma for a single-node tuple.
- Use a stable `GRAPH_CONFIG_ID` identifying this graph configuration, not a session or request.
- Create **one shared pool and engine**, once at FastAPI lifespan startup. Never create/destroy pools in routes, session helpers, or reset handlers. A stopped native pool must not be rebuilt in the same process.
- `PIPELINE_JSON_PATH` points to the **FlowX planning config**, not a native graph document. Read and parse it **once at lifespan startup** into `workflow_config`. Require a JSON object; default a missing `pool_size` to `4` for older planning files. Require a positive integer, explicitly rejecting booleans, strings, floats, null, zero, and negative values. Do not read pool size from an environment variable.
- Derive a separate native graph JSON string from the imported `NODES`: each node record uses `name=Node.STEP_ID`, `type=Node.__name__`, `depends=list(Node.DEPENDENCIES)`, `meta_type="GNode"`, and `loop=1`. Validate unique non-empty step IDs, dependency IDs, missing dependencies and DAG cycles before constructing the pool. Never copy planning `type`, `meta_type`, `depends`, or `loop` fields into native records.
- Map native JSON node names directly to concrete zero-argument node classes. Do not introduce factories, partials or wrapper classes, register classes globally, use registry-based JSON loading, or clone native nodes.
- Construct `WorkflowPipelinePool(pipeline_json=native_json_content, node_classes=NODE_CLASSES, size=workflow_config["pool_size"])`, passing the map unchanged. Use JSON **content**, never a filename, the raw planning document, `nodes=`, or `pipelines=`.
- Construct `WorkflowEngine(graph_config_id=GRAPH_CONFIG_ID, steps_meta=STEP_CHAIN, pool=PIPELINE_POOL)`.
- Set `app = FastAPI(title=..., lifespan=lifespan)`. In lifespan cleanup call `ENGINE.close()` **before** `PIPELINE_POOL.close()`, including error paths. Run blocking cleanup outside the async event loop.
- A paused request retains its pipeline lease. Allow enough pool slots for concurrent waiting requests using the planning config's `pool_size` (default 4).
- Node subclasses explicitly define/register `self.setWaitFunc(self.wait_function)` when they need to wait, reporting `awaiting_input`/`running` through `WorkflowParam`. `INPUT_REQUIRED` is metadata only. Neither the main entrypoint nor the engine installs gates or implements a replacement scheduler.

## HTTP compatibility

- Preserve `RunStepInput` fields `sessionId`, `stepId`, flexible `input: str | dict[str, Any] | None`, and optional `file_path`.
- Preserve `ResetSessionInput.sessionId` and `ResetSessionOutput` fields `ok`, `sessionId`, `threadId`, `runId`.
- Keep a synchronized `SESSION_REQUESTS` mapping from session IDs to opaque request IDs. Reuse a request ID while a native graph is waiting, so later step inputs continue the same run. The engine forgets finished runs; submitting their ID starts a fresh run.
- `_get_engine()` returns the lifespan-managed engine; before startup or after shutdown it returns HTTP 503, never lazily creating a pool/engine.
- `GET /` returns a simple health payload.
- `POST /api/run-step` validates `stepId` against `STEP_CHAIN` (unknown IDs return HTTP 404). Identify file nodes by `metaNodeKind == "WorkflowFileNode"`; current `step_meta()` has no `extData`. Decode a supplied `file_path` or one path/base64 upload in `payload.input` to bytes before calling the engine. Accept empty file bytes, reject invalid or multiple uploads with HTTP 422, and preserve absent input as `None` so explicit wait gates can pause. File nodes own storage/processing; they do not parse path dictionaries. For other node kinds pass `payload.input` unchanged.
- Stream `engine.events(request_id, {step_id: normalized_input})` through `StreamingResponse`, media type `text/event-stream`, headers `Cache-Control: no-cache` and `Connection: keep-alive`. Do not advance nodes manually.
- `POST /api/reset-session`, with `response_model=ResetSessionOutput`, calls `engine.close(old_request_id)` only for this session's existing request, assigns a new request ID, and leaves the shared engine/pool intact. Never pass `None` here (that would close all runs). Return `threadId=GRAPH_CONFIG_ID` and `runId=new_request_id` to preserve legacy HTTP field names without inventing engine compatibility attributes.
- End with `if __name__ == "__main__": uvicorn.run(app, ...)`.

## Forbidden legacy API

Do not pass `pipeline_json_path`, `thread_id`, or `session` to `WorkflowEngine`. Do not use `engine.thread_id`, `engine.session`, `engine.reset_session()` or `engine._run_step_events()`. Do not add shims for these removed APIs.

## Output

Return only runnable Python code, without Markdown fences or explanatory prose. Do not add unrelated routes, CLI parsing, or extra framework scaffolding.
