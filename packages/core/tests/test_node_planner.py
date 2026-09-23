import json
from types import SimpleNamespace

from flowx_core.architect.node_planner import NodePlanner
from flowx_core.tools.workflow_node_reference import (
	render_subclass_guidance_method_signatures,
	render_workflow_method_signatures,
	resolve_workflow_node_reference,
)


class _FakeCompletions:
	def __init__(self, responses):
		self._responses = list(responses)

	def create(self, **kwargs):
		content = self._responses.pop(0)
		return SimpleNamespace(
			choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
		)


class _FakeClient:
	def __init__(self, responses):
		self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


def _write_skill(skills_root, skill_name: str, body: str = "# Skill\n\nsearch helper"):
	skill_dir = skills_root / skill_name
	skill_dir.mkdir(parents=True, exist_ok=True)
	(skill_dir / "skill.md").write_text(body, encoding="utf-8")


def test_node_context_lists_selectable_types_and_skill_descriptions(tmp_path) -> None:
	skills_root = tmp_path / "skills"
	_write_skill(skills_root, "baidu_search", "# Skill\n\nBaidu search helper")
	planner = NodePlanner(client=_FakeClient([]), skills_root_path=str(skills_root))

	context = planner._node_context(
		{
			"name": "SearchSkill",
			"desc": "search for external data",
			"depends": [],
			"ext_data": {
				"type": "skill",
				"desc": "use a search skill",
				"skill_name": "baidu_search",
			},
			"inputs_format": {"query": "string"},
		},
		1,
	)

	assert "- selectable node types:" in context
	assert "user_input -> WorkflowStepNode" in context
	assert "service ->" not in context
	assert "skill -> WorkflowSkillNode" in context
	assert "spatial_temporal_contract -> SpatialTemporalContractNode" in context
	assert "- available skills:" in context
	assert "baidu_search:" in context


def test_node_context_omits_legacy_service_metadata(tmp_path) -> None:
	planner = NodePlanner(client=_FakeClient([]), skills_root_path=str(tmp_path / "skills"))

	context = planner._node_context(
		{
			"name": "ComputeResult",
			"desc": "compute the final score",
			"depends": [],
			"ext_data": {
				"type": "none",
				"desc": "no need for ext data",
			},
		},
		1,
	)

	assert "- meta_node_kind: WorkflowOperationNode" in context
	assert "recommended base class: WorkflowOperationNode" in context
	assert "main utility methods: process_operation(dependency_results, session_state)" in context
	assert "StepRunOutput schema methods: process_operation(dependency_results, session_state)" in context
	assert "subclass implementation hooks: process_operation(dependency_results, session_state)" in context
	assert "service_name" not in context
	assert "- services:" not in context


def test_node_context_recommends_spatial_temporal_contract_node(tmp_path) -> None:
	planner = NodePlanner(client=_FakeClient([]), skills_root_path=str(tmp_path / "skills"))
	spatial_reference = resolve_workflow_node_reference(meta_node_kind="SpatialTemporalContractNode")
	spatial_hook = render_workflow_method_signatures(
		spatial_reference.base_class,
		spatial_reference.subclass_implementation_methods,
	)[0]
	spatial_guidance_hooks = render_subclass_guidance_method_signatures(
		spatial_reference.base_class,
		spatial_reference.subclass_implementation_methods,
	)

	context = planner._node_context(
		{
			"name": "BuildContract",
			"desc": "generate a spatial-temporal contract from upstream scene text",
			"depends": ["DescribeScene"],
			"ext_data": {
				"type": "spatial_temporal_contract",
				"desc": "build contract json from upstream description",
			},
		},
		1,
	)

	assert "recommended base class: SpatialTemporalContractNode" in context
	assert "- meta_node_kind: SpatialTemporalContractNode" in context
	assert "StepRunOutput schema methods: process_operation(dependency_results, session_state)" in context
	assert f"subclass implementation hooks: {spatial_hook}" in context
	assert spatial_guidance_hooks
	for guidance_hook in spatial_guidance_hooks:
		assert guidance_hook in context


def test_amend_graph_node_from_files_updates_graph_and_regenerates_node_plan(tmp_path) -> None:
	requirement_path = tmp_path / "requirement.md"
	requirement_path.write_text("# Requirement\n\nCollect a search query and call a skill.", encoding="utf-8")
	skills_root = tmp_path / "skills"
	_write_skill(skills_root, "baidu_search", "# Skill\n\nBaidu search helper")

	graph_path = tmp_path / "graph_plan.json"
	graph_path.write_text(
		json.dumps(
			{
				"nodes": [
					{
						"name": "SearchSkill",
						"type": "SearchSkill",
						"desc": "search for data",
						"enable": True,
						"depends": [],
						"ext_data": {
							"type": "skill",
							"desc": "search via skill",
							"skill_name": "baidu_search",
						},
						"inputs_format": {"query": "string"},
					}
				]
			},
			ensure_ascii=False,
			indent=2,
		),
		encoding="utf-8",
	)

	node_plan_path = tmp_path / "node_docs" / "SearchSkill.md"
	planner = NodePlanner(
		skills_root_path=str(skills_root),
		client=_FakeClient(
			[
				json.dumps(
					{
						"name": "SearchSkill",
						"type": "SearchSkill",
						"desc": "collect query and search with optional locale",
						"enable": True,
						"depends": [],
						"ext_data": {
							"type": "skill",
							"desc": "search via skill",
							"skill_name": "baidu_search",
						},
						"inputs_format": {"query": "string", "locale": "string"},
					},
					ensure_ascii=False,
				),
				"# Node Brief\n\n## What This Node Achieves\nCollects a query and locale, then uses the selected skill.\n\n## Core Functions\n- process_operation: runs the skill using the validated user input.\n",
			]
		)
	)

	amended_graph_path, regenerated_plan_path = planner.amend_graph_node_from_files(
		node_name="SearchSkill",
		user_prompt="Add locale as an optional user input field and update the description.",
		requirement_md_path=str(requirement_path),
		graph_plan_json_path=str(graph_path),
		node_output_path=str(node_plan_path),
	)

	amended_graph = json.loads(amended_graph_path.read_text(encoding="utf-8"))
	amended_node = amended_graph["nodes"][0]

	assert amended_graph_path == graph_path
	assert regenerated_plan_path == node_plan_path
	assert amended_node["ext_data"]["type"] == "skill"
	assert amended_node["ext_data"]["skill_name"] == "baidu_search"
	assert amended_node["inputs_format"] == {"query": "string", "locale": "string"}
	assert "collect query and search with optional locale" == amended_node["desc"]
	assert "Collects a query and locale" in node_plan_path.read_text(encoding="utf-8")