from types import SimpleNamespace

import pytest

from flowx_core.builder_services import GraphBuildService
from flowx_core.demand_analyzer.requirement_disector import RequirementDisector


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


def test_analyze_writes_markdown(tmp_path):
    output_path = tmp_path / "requirement_analysis.md"
    analyzer = RequirementDisector(
        client=_FakeClient(
            ["# 需求分析\n\n- 每天早上 9 点执行一次同步任务"]
        )
    )

    result = analyzer.analyze("每天早上9点同步一次数据", str(output_path))

    assert result.output_path == output_path
    assert output_path.read_text(encoding="utf-8") == "# 需求分析\n\n- 每天早上 9 点执行一次同步任务"


def test_requirement_disector_prompt_uses_backend_only_node_design_columns() -> None:
    analyzer = RequirementDisector(client=_FakeClient([]))

    assert "节点ID | meta_node_kind | ext_data.type | 作用 | depends" in analyzer.system_prompt
    assert "ag_ui_workflow Base Step Metas" in analyzer.system_prompt
    assert "show_frontend" not in analyzer.system_prompt


def test_process_input_writes_from_user_input(tmp_path):
    analyzer = RequirementDisector(client=_FakeClient(["# 需求分析\n新需求"]))
    output_path = tmp_path / "analysis"

    result = analyzer.process_input("新需求", {}, {
        "RequirementDisector::action": "write",
        "requirement_disector": {"output_path": str(output_path)},
    })

    assert result.derived == {"requirement_md_path": str(output_path.with_suffix(".md"))}
    assert output_path.with_suffix(".md").read_text(encoding="utf-8") == "# 需求分析\n新需求"


def test_process_input_amends_existing_analysis(tmp_path, monkeypatch):
    output_path = tmp_path / "analysis.md"
    output_path.write_text("# Original analysis", encoding="utf-8")
    analyzer = RequirementDisector(client=_FakeClient([]))
    calls = []
    monkeypatch.setattr(analyzer, "code_to_file", lambda prompt, path, **kwargs: calls.append((prompt, path, kwargs)) or output_path)

    result = analyzer.process_input("", {}, {
        "RequirementDisector::action": "amend",
        "requirement_disector": {"output_path": str(output_path), "amendment": "Clarify the scope"},
    })

    assert result.derived == {"requirement_md_path": str(output_path)}
    assert "# Original analysis" in calls[0][0]
    assert "Clarify the scope" in calls[0][0]


@pytest.mark.parametrize("session_state, message", [
    ({}, "requirement_disector"),
    ({"requirement_disector": {"action": "write"}}, "RequirementDisector::action"),
    ({"RequirementDisector::action": "plan", "requirement_disector": {}}, "RequirementDisector::action"),
    ({"RequirementDisector::action": "write", "requirement_disector": {}}, "output_path"),
    ({"RequirementDisector::action": "write", "requirement_disector": {"output_path": "analysis.md"}}, "requirement_text"),
    ({"RequirementDisector::action": "amend", "requirement_disector": {"output_path": "analysis.md"}}, "amendment"),
])
def test_process_input_rejects_invalid_request(session_state, message):
    with pytest.raises(ValueError, match=message):
        RequirementDisector(client=_FakeClient([])).process_input("", {}, session_state)


def test_process_input_amend_requires_existing_file(tmp_path):
    analyzer = RequirementDisector(client=_FakeClient([]))
    with pytest.raises(FileNotFoundError, match="Requirement analysis not found"):
        analyzer.process_input("", {}, {
            "RequirementDisector::action": "amend",
            "requirement_disector": {"output_path": str(tmp_path / "missing.md"), "amendment": "Change"},
        })


def test_graph_build_service_uses_process_input_for_analysis(tmp_path):
    analyzer = RequirementDisector(client=_FakeClient(["# Analysis"]))
    builder = SimpleNamespace(
        analyzer=analyzer, root_dir=str(tmp_path), requirement_md_path=None,
        _logger=SimpleNamespace(info=lambda *args: None, debug=lambda *args: None),
    )

    output_path = GraphBuildService(builder).analyze_requirement("Build a workflow")

    assert output_path == str(tmp_path / "requirement_analysis.md")
    assert (tmp_path / "requirement_analysis.md").read_text(encoding="utf-8") == "# Analysis"
