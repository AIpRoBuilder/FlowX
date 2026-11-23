<!-- FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
Moved 2026-11-22 from architect/prompts/graph_planner_prompt.md and adapted from an LLM system prompt into a contract for a coding agent.
Modified 2026-10-13: request-scoped nodes complete once; do not promise native repeated execution. -->
# Graph contract

You plan the workflow graph from the requirement analysis. Submit one JSON object `workflow.json`.

## Shape

```json
{
  "nodes": [
    {
      "name": "ClassifyTicket",
      "type": "ClassifyTicket",
      "meta_node_kind": "WorkflowStepNode",
      "desc": "classify the support ticket into a category",
      "ext_data": {"type": "user_input", "desc": "the ticket text from the user"},
      "inputs_format": {"ticket": "string"},
      "enable": true,
      "loop": 1
    },
    {
      "name": "DraftReply",
      "type": "DraftReply",
      "meta_node_kind": "WorkflowStepNode",
      "desc": "draft a reply from the category and priority",
      "ext_data": {"type": "none", "desc": "no need for ext data"},
      "enable": true,
      "depends": ["ClassifyTicket"]
    }
  ]
}
```

## Node fields

- `name`: unique English identifier (letters and digits, starts with a letter, PascalCase recommended) that carries
  business meaning (`FileUpload`, `ImageAnalysis`, `ReportGeneration`). Placeholder names (`Node1`, `MyNode`, `A`) are
  not allowed. It must not shadow a Python standard-library or runtime module.
- `type`: identical to `name`.
- `meta_node_kind`: a value from the node-type catalog in `contract.node_types`; later stages use it as the only base
  class selector.
- `desc`: what the node does.
- `ext_data`: required object `{"type": "...", "desc": "..."}`.
  - `WorkflowStepNode`: interactive nodes use `user_input`; automatic nodes use `none` or a domain source (`url`,
    `file`, `db`). With type `none`, `desc` is `no need for ext data`.
  - `WorkflowFileNode`: `ext_data.type` must be `user_file_input`.
  - `ext_data.type = "image"` is deprecated. Use `user_file_input` for uploads, `url` for remote image/video APIs and
    `none` for local resize/crop/FFmpeg/Blender processing.
- `enable`: boolean.
- `loop`: only 1 (or omit it). A node completes once per request; loop inside the node's code or start a new request.
- `depends`: names of upstream nodes; omit when there are none.
- `inputs_format`: for `user_input` nodes, an object mapping input fields to `string|number|boolean|object|array`.

## Rules of thumb

- Map modules and functions from the requirement's node design table to nodes; keep them few.
- Derive dependencies from the flow and interaction order; keep unrelated modules independent so they can run in
  parallel.
- The graph is one connected, acyclic component. Enabled nodes never depend on disabled nodes.
- Names and types are always English, even when descriptions are not.
