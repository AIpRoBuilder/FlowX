# SPDX-FileCopyrightText: 2026 FlowX contributors
# SPDX-License-Identifier: Apache-2.0
# FlowX-Origin: urn:uuid:ea62b8c8-902a-4eb4-a42e-e5412ed08466
# Upstream: https://github.com/AIpRoBuilder/FlowX
# Modified 2026-11-23: add graph.edit_noop and builder.integrity rules.
"""Structured audit findings and the rule catalog shared by contracts and auditors."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

GENERIC_REMEDIATION = "Fix the reported problem and submit the artifact again."


@dataclass(frozen=True)
class Rule:
    """One named contract rule: the text shown in a contract and the fix hint for a finding."""

    rule_id: str
    text: str
    remediation: str = GENERIC_REMEDIATION


@dataclass(frozen=True)
class Finding:
    """One audit or test problem, addressed precisely enough to repair in a single turn."""

    rule_id: str
    message: str
    file: str = ""
    line: int = 0
    severity: str = "error"
    remediation: str = GENERIC_REMEDIATION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rules(*rules: Rule) -> dict[str, Rule]:
    return {rule.rule_id: rule for rule in rules}


CATALOG: dict[str, Rule] = _rules(
    # Requirement document.
    Rule("requirement.non_empty", "The requirement analysis must be a non-empty Markdown document.",
         "Write the requirement analysis as Markdown with the required sections."),
    Rule("requirement.sections",
         "The document needs these level-2 sections: Background, Goals, Scope and Functional Details "
         "(Chinese headings 需求背景, 需求目标, 需求范围, 功能详细说明 are accepted).",
         "Add every missing `## ` section; keep the node design table inside Functional Details."),
    Rule("requirement.node_design",
         "Functional Details must contain a node design table with columns node id, meta_node_kind, "
         "ext_data.type, purpose and depends.",
         "Add a Markdown table listing each node with those columns."),
    # Graph.
    Rule("graph.json", "workflow.json must be one JSON object: {\"nodes\": [...]}.",
         "Submit a JSON object with a non-empty `nodes` array."),
    Rule("graph.node_fields",
         "Every node needs name, type (identical to name), meta_node_kind, desc and ext_data {type, desc}.",
         "Add the missing fields to the node."),
    Rule("graph.node_name",
         "Node names are unique English identifiers (letters and digits, starting with a letter) that do "
         "not shadow Python or runtime modules.",
         "Rename the node to a unique semantic PascalCase identifier."),
    Rule("graph.dependencies", "`depends` lists existing node names; no self or duplicate dependencies.",
         "Fix the dependency list so every entry names another defined node once."),
    Rule("graph.enabled", "An enabled node cannot depend on a disabled node.",
         "Enable the dependency or disable the dependent node."),
    Rule("graph.loop", "Nodes complete once per request: `loop` must be 1 or omitted.",
         "Remove `loop` or set it to 1; loop inside the node's own code if needed."),
    Rule("graph.name_type_mismatch", "A node's `type` must equal its `name`.",
         "Set `type` to the same string as `name`."),
    Rule("graph.name_not_english_identifier",
         "Node names use English letters and digits only and start with a letter.",
         "Rename the node, for example to PascalCase."),
    Rule("graph.meta_node_kind_invalid", "`meta_node_kind` must be one of the kinds in the node-type catalog.",
         "Pick a kind from `contract.node_types`."),
    Rule("graph.meta_node_kind_ext_data_mismatch",
         "`meta_node_kind` must agree with `ext_data.type` (user_input / user_file_input).",
         "Use WorkflowStepNode with user_input or WorkflowFileNode with user_file_input."),
    Rule("graph.inputs_format",
         "`user_input` nodes need a non-empty `inputs_format` mapping of field names to "
         "string, number, integer, boolean, object, array or null.",
         "Add `inputs_format` such as {\"ticket\": \"string\"}; do not declare it for file nodes."),
    Rule("graph.image_ext_type_unsupported", "`ext_data.type='image'` is deprecated.",
         "Use user_file_input for uploads, url for provider APIs, none for local processing."),
    Rule("graph.cycle_detected", "The graph must be acyclic.", "Remove one dependency from the cycle."),
    Rule("graph.disconnected", "The graph must be one connected component.",
         "Connect the isolated node or merge it into the flow."),
    Rule("graph.edit_noop", "A graph edit must change workflow.json relative to the current baseline.",
         "Submit a modified workflow.json that implements the requested change, or abort the edit."),
    # Node source.
    Rule("node.source_missing", "A node submission must include `code` (the node's Python module).",
         "Submit the `code` artifact."),
    Rule("node.test_missing", "A node needs a pytest module (`test`) before it can be published.",
         "Submit the `test` artifact next to the code."),
    Rule("node.syntax_error", "The node module must be valid Python.", "Fix the syntax error at the given line."),
    Rule("node.compile_error", "The node module must compile.", "Fix the compile error at the given line."),
    Rule("node.class_missing", "The module must define the node class.", "Define a class named like the file."),
    Rule("node.no_todo_markers", "TODO markers are not allowed in node files.",
         "Implement the behavior or remove the marker."),
    Rule("node.class_name_file_prefix_mismatch", "The node class name must equal the node (file) name.",
         "Rename the class to the node name."),
    Rule("node.step_id_missing", "The class must define STEP_ID.", "Add `STEP_ID = \"<ClassName>\"`."),
    Rule("node.step_id_mismatch", "STEP_ID must equal the class name.", "Set STEP_ID to the class name."),
    Rule("node.dependencies_mismatch", "DEPENDENCIES must equal the node's `depends` in workflow.json.",
         "Copy the node's `depends` list into DEPENDENCIES."),
    Rule("node.dependencies_missing", "The class must define DEPENDENCIES.", "Add DEPENDENCIES as a list of step ids."),
    Rule("node.session_state_ancestor_key_invalid",
         "Only read session_state keys that an ancestor node writes.",
         "Read an ancestor's key (see `context.ancestors[*].session_state_keys`) or stop reading the key."),
    Rule("node.dependency_results_derived_key_invalid",
         "Read upstream values only through keys the dependency returns in `derived`.",
         "Use a key listed in `context.ancestors[*].derived_keys`."),
    Rule("node.meta_node_kind_requires_matching_base_class",
         "The base class must match the node's meta_node_kind.", "Subclass the class named by `meta`."),
    Rule("node.workflow_file_node_process_input_missing",
         "WorkflowFileNode subclasses must implement process_input(self, files: dict[str, bytes], dependency_results, session_state).",
         "Implement synchronous process_input(self, files: dict[str, bytes], dependency_results, session_state) returning StepRunOutput."),
    Rule("node.test_failed", "The node's pytest module must pass (media calls run in dry-run mode).",
         "Fix the node or the test using the log in `context.last_test_log`."),
    Rule("node.test_timeout", "The node test must finish within the time limit.",
         "Remove long-running or network work from the test."),
    # Entrypoint.
    Rule("main.render_failed", "The entrypoint is rendered from the published nodes.",
         "Fix the failing node or the workflow graph and rebuild."),
    Rule("main.audit_failed", "The rendered entrypoint must pass the entrypoint audit.",
         "Fix the node set so the entrypoint renders correctly."),
    Rule("main.smoke_failed", "The entrypoint's smoke run must not report errors.",
         "Fix the node that the run log points to and rebuild."),
    # Builder internals.
    Rule("builder.bad_submission", "`artifacts` maps the task's slot names to text (the graph may be a JSON object).",
         "Submit only the slot names listed in the task's `submit.slots`, each as a string."),
    Rule("builder.integrity", "FlowX-owned workflow files must not be edited outside FlowX.",
         "Do not edit files under <workspace>/workflows/ directly; use FlowX begin_build or begin_evolve."),
    Rule("builder.internal_error", "The builder could not validate the submission.",
         "Retry; if it persists report the message."),
)


def finding(
    rule_id: str,
    message: str,
    *,
    file: str = "",
    line: int = 0,
    severity: str = "error",
    remediation: str | None = None,
) -> Finding:
    """Build a finding, filling the remediation from the catalog."""
    rule = CATALOG.get(rule_id)
    return Finding(
        rule_id=rule_id,
        message=message,
        file=file,
        line=int(line or 0),
        severity=severity,
        remediation=remediation if remediation is not None else (rule.remediation if rule else GENERIC_REMEDIATION),
    )


def from_violation(violation: Any, *, namespace: str, file: str = "") -> Finding:
    """Convert an auditor ``RuleViolation`` / ``JsonRuleViolation`` (or its dict form)."""
    if isinstance(violation, Mapping):
        rule, detail, line = violation.get("rule", ""), violation.get("detail", ""), violation.get("lineno", 0)
    else:
        rule = getattr(violation, "rule", "")
        detail = getattr(violation, "detail", "")
        line = getattr(violation, "lineno", 0)
    return finding(f"{namespace}.{rule}", str(detail), file=file, line=int(line or 0))


def from_violations(violations: Iterable[Any], *, namespace: str, file: str = "") -> list[Finding]:
    return [from_violation(item, namespace=namespace, file=file) for item in violations]


def rules_for(namespace: str) -> list[dict[str, str]]:
    """Catalog entries whose id starts with ``namespace.`` as contract rule dicts."""
    prefix = f"{namespace}."
    return [
        {"rule_id": rule.rule_id, "text": rule.text}
        for rule in CATALOG.values()
        if rule.rule_id.startswith(prefix)
    ]


__all__ = [
    "CATALOG",
    "Finding",
    "Rule",
    "finding",
    "from_violation",
    "from_violations",
    "rules_for",
]
