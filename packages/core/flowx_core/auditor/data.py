from dataclasses import dataclass


@dataclass
class RuleViolation:
	class_name: str
	rule: str
	detail: str
	lineno: int

@dataclass
class JsonRuleViolation:
	parts_name: str
	rule: str
	detail: str
	lineno: int