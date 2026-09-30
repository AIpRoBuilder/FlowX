from types import SimpleNamespace

import pytest
from ag_ui_workflow import StepRunOutput

from flowx_core.architect.node_planner import NodePlanElement
from flowx_core.llm_client.coder import Coder


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
	planner = NodePlanElement(client=_FakeClient([]), skills_root_path=str(skills_root))

	context = planner.render_node_context(
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
	assert "- available skills:" in context
	assert "baidu_search:" in context


def test_node_context_omits_legacy_service_metadata(tmp_path) -> None:
	planner = NodePlanElement(client=_FakeClient([]), skills_root_path=str(tmp_path / "skills"))

	context = planner.render_node_context(
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

	assert "- meta_node_kind: WorkflowStepNode" in context
	assert "recommended base class: WorkflowStepNode" in context
	assert "main utility methods: process_input(user_input, dependency_results, session_state)" in context
	assert "StepRunOutput schema methods: process_input(user_input, dependency_results, session_state)" in context
	assert "subclass implementation hooks: process_input(user_input, dependency_results, session_state)" in context
	assert "service_name" not in context
	assert "- services:" not in context


def test_plan_node_writes_only_requested_node(tmp_path) -> None:
	output_path = tmp_path / "docs" / "Selected.md"
	planner = NodePlanElement(client=_FakeClient(["# Node Brief\n\nSelected node"]),
			session_marking_prompt="Use request-scoped IO")

	written = planner.plan_node(
		{"name": "Selected", "desc": "process input", "ext_data": {"type": "none"}},
		"# Requirements", output_path,
	)

	assert written == output_path
	assert output_path.read_text(encoding="utf-8") == "# Node Brief\n\nSelected node"
	assert list(output_path.parent.iterdir()) == [output_path]
	assert "Use request-scoped IO" in planner.system_prompt


def test_node_plan_element_uses_inherited_coder(tmp_path) -> None:
	client = _FakeClient(["# Node Brief"])
	planner = NodePlanElement(
		client=client, use_streaming=False,
		session_marking_prompt="Keep plan files request scoped.",
	)

	assert isinstance(planner, Coder)
	assert NodePlanElement.code_to_file is Coder.code_to_file
	assert planner.client is client
	assert planner.system_prompt.count("Keep plan files request scoped.") == 1
	assert not hasattr(planner, "_coder")
	assert planner.plan_node({"name": "Task"}, "Requirements", tmp_path / "Task.md").read_text() == "# Node Brief"


def test_plan_node_respects_overwrite_flag(tmp_path) -> None:
	output_path = tmp_path / "Selected.md"
	output_path.write_text("keep", encoding="utf-8")
	planner = NodePlanElement(client=_FakeClient([]))

	with pytest.raises(FileExistsError):
		planner.plan_node({"name": "Selected"}, "requirements", output_path, overwrite=False)

	assert output_path.read_text(encoding="utf-8") == "keep"


def test_configured_node_plan_step_processes_one_node(tmp_path) -> None:
	output_path = tmp_path / "Selected.md"
	step = NodePlanElement(
		client=_FakeClient(["# Node Brief"]),
		node={"name": "Selected", "ext_data": {"type": "none"}},
		requirement_text="Requirements", output_path=str(output_path),
	)

	result = step.process_input("", {}, {})

	assert result.derived["output_path"] == str(output_path)
	assert output_path.read_text(encoding="utf-8") == "# Node Brief"


def test_process_input_includes_only_direct_dependency_plans(tmp_path) -> None:
	class RecordingCompletions(_FakeCompletions):
		def create(self, **kwargs):
			self.prompt = kwargs["messages"][-1]["content"]
			return super().create(**kwargs)

	completion = RecordingCompletions(["# Node Brief"])
	client = SimpleNamespace(chat=SimpleNamespace(completions=completion))
	upstream = tmp_path / "Upstream.md"
	upstream.write_text("Upstream result", encoding="utf-8")
	unrelated = tmp_path / "Unrelated.md"
	unrelated.write_text("Unrelated result", encoding="utf-8")
	step = NodePlanElement(
		client=client, node={"name": "Target", "depends": ["Upstream"]},
		requirement_text="Requirements", output_path=str(tmp_path / "Target.md"),
	)

	step.process_input("", {
		"Upstream::audit": StepRunOutput(derived={"markdown_path": str(upstream)}),
		"Unrelated::audit": StepRunOutput(derived={"markdown_path": str(unrelated)}),
	}, {})

	assert "Upstream result" in completion.prompt
	assert "Unrelated result" not in completion.prompt


def test_process_input_amends_existing_node_plan_with_dependencies(tmp_path) -> None:
	plan = tmp_path / "Target.md"
	plan.write_text("Original plan", encoding="utf-8")
	upstream = tmp_path / "Upstream.md"
	upstream.write_text("Upstream result", encoding="utf-8")

	class RecordingCompletions(_FakeCompletions):
		def create(self, **kwargs):
			self.prompt = kwargs["messages"][-1]["content"]
			return super().create(**kwargs)

	completion = RecordingCompletions(["Amended plan"])
	client = SimpleNamespace(chat=SimpleNamespace(completions=completion))
	step = NodePlanElement(
		client=client, node={"name": "Target", "depends": ["Upstream"]},
		requirement_text="Requirements", output_path=str(plan), amendment="Make it shorter",
	)
	result = step.process_input("", {
		"Upstream::audit": StepRunOutput(derived={"markdown_path": str(upstream)}),
	}, {})

	assert plan.read_text(encoding="utf-8") == "Amended plan"
	assert "Original plan" in completion.prompt
	assert "Upstream result" in completion.prompt
	assert "Make it shorter" in completion.prompt
	assert result.derived["markdown_path"] == str(plan.resolve())


def test_process_input_generates_missing_plan_before_amending(tmp_path) -> None:
	plan = tmp_path / "Target.md"
	step = NodePlanElement(
		client=_FakeClient(["Initial plan", "Amended plan"]),
		node={"name": "Target"}, requirement_text="Requirements",
		output_path=str(plan), amendment="Refine the brief",
	)

	result = step.process_input("", {}, {})

	assert plan.read_text(encoding="utf-8") == "Amended plan"
	assert result.derived["markdown_path"] == str(plan.resolve())


def test_process_input_skips_generation_when_disabled(tmp_path) -> None:
	plan = tmp_path / "Target.md"
	step = NodePlanElement(
		client=_FakeClient([]), node={"name": "Target"},
		output_path=str(plan), generate_markdown=False,
	)

	result = step.process_input("", {}, {})

	assert result.derived["markdown_path"] is None
	assert not plan.exists()


def test_process_input_dispatches_amendment_from_session_state(tmp_path) -> None:
	plan = tmp_path / "Target.md"
	plan.write_text("Original plan", encoding="utf-8")
	step = NodePlanElement(
		client=_FakeClient(["Updated plan"]), node={"name": "Target"},
		output_path=str(plan),
	)
	plan_request = {
		"generate_markdown": False, "amendment": "Improve the brief",
	}

	result = step.process_input("", {}, {"node_plan_request": plan_request})

	assert plan.read_text(encoding="utf-8") == "Updated plan"
	assert result.derived["markdown_path"] == str(plan.resolve())


def test_process_input_rejects_invalid_session_state_request(tmp_path) -> None:
	step = NodePlanElement(
		client=_FakeClient([]), node={"name": "Target"},
		output_path=str(tmp_path / "Target.md"),
	)
	with pytest.raises(TypeError, match="string amendment"):
		step.process_input("", {}, {"node_plan_request": {"amendment": 123}})