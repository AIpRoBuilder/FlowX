<!-- FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
Moved 2026-11-22 from demand_analyzer/prompts/requirement_disector_prompt.md and adapted from an LLM system prompt into a contract for a coding agent. -->
# Requirement contract

You write the requirement analysis that every later stage reads. Submit one Markdown document.

## Required sections (level-2 headings)

1. `## Background` - the problem being solved (1-3 points) and alternatives, marking unknowns `TBD`.
2. `## Goals` - the intended outcome and how success is verified; state assumptions when data is missing.
3. `## Scope` - a table with columns `module | requirement | description | priority` (High/Medium/Low).
4. `## Functional Details` - keep it minimal:
   - the smallest product flow (3-6 steps);
   - the smallest interaction flow (main path plus one error path);
   - the node design table.

Chinese headings (需求背景, 需求目标, 需求范围, 功能详细说明) are accepted in place of the English ones.

## Node design table

Columns: `node id | meta_node_kind | ext_data.type | purpose | depends`.

- Design nodes only from the node-type catalog in `contract.node_types`; every row names the `meta_node_kind` of a
  catalog entry. Do not invent node capabilities outside the catalog.
- `WorkflowStepNode` rows use `ext_data.type` `user_input`, `none`, or a domain source such as `url`, `file`, `db`.
  `WorkflowFileNode` rows use `user_file_input`.
- Use the fewest nodes that cover the flow; avoid redundant nodes and repeated explanations.
- Media work: uploads are `WorkflowFileNode` + `user_file_input`; remote image/video APIs use `url`; local resize,
  crop, FFmpeg or Blender processing uses `none`.

## Writing rules

- Be concise and executable. Mark unknowns `TBD` and state the assumption.
- Output Markdown only; no code fences around the whole document.
