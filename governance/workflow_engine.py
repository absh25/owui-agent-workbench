from __future__ import annotations

from pathlib import Path
from typing import Any
from fnmatch import fnmatchcase

from .models import ToolExecutionContext, WorkflowDecision
from .policy_loader import PolicyError, load_yaml


def _matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if set(expected) != {"in"} or not isinstance(expected["in"], list):
            raise PolicyError("Only the {'in': [...]} argument operator is supported")
        return any(_matches(actual, candidate) for candidate in expected["in"])
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.casefold() == expected.casefold()
    return actual == expected


class WorkflowEngine:
    """Small, deterministic v1 rule engine: tool, equality, and `in`."""

    def __init__(self, workflow_paths: dict[str, str | Path]):
        self.workflow_paths = {
            name: Path(path) for name, path in workflow_paths.items()
        }

    def evaluate(
        self,
        workflow_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> WorkflowDecision:
        path = self.workflow_paths.get(workflow_name)
        if path is None:
            raise PolicyError(f"Unknown workflow: {workflow_name}")
        document = load_yaml(path)
        version = str(document.get("version", "unknown"))
        rules = document.get("rules") or []
        if not isinstance(rules, list):
            raise PolicyError("workflow rules must be a list")

        for rule in rules:
            if not isinstance(rule, dict):
                raise PolicyError("each workflow rule must be a mapping")
            configured_tool = rule.get("tool")
            if configured_tool is not None and not fnmatchcase(
                tool_name, str(configured_tool)
            ):
                continue
            expected_arguments = rule.get("arguments") or {}
            if not isinstance(expected_arguments, dict):
                raise PolicyError("rule arguments must be a mapping")
            if not all(
                _matches(arguments.get(key), expected)
                for key, expected in expected_arguments.items()
            ):
                continue
            approver_group = _resolve_approver_group(rule.get("approver_group"), context)
            return WorkflowDecision(
                decision=str(rule.get("decision", "review")).upper(),  # type: ignore[arg-type]
                reason=rule.get("reason"),
                matched_rule=str(rule.get("id", "unnamed-rule")),
                approver_group=approver_group,
                workflow_version=version,
            )

        default = document.get("default") or {}
        if isinstance(default, str):
            default = {"decision": default}
        if not isinstance(default, dict):
            raise PolicyError("workflow default must be a mapping or decision string")
        return WorkflowDecision(
            decision=str(default.get("decision", "review")).upper(),  # type: ignore[arg-type]
            reason=default.get("reason", "No workflow rule matched; manual review required."),
            matched_rule="default",
            approver_group=_resolve_approver_group(default.get("approver_group"), context),
            workflow_version=version,
        )


def _resolve_approver_group(configured: Any, context: ToolExecutionContext) -> Any:
    """Resolve identity-aware approval targets from the trusted request context.

    ``current_user_group_admin`` deliberately stores the group target, rather
    than trusting a group supplied in tool arguments.  The approval UI/service
    can then verify that the acting reviewer is an administrator of that group.
    """
    if configured == "current_user":
        if not context.user_id:
            raise PolicyError("current_user confirmation requires an authenticated requester")
        return f"user:{context.user_id}"
    if not isinstance(configured, str) or configured not in {
        "current_user_group_admin",
        "requester_group_admin",
    }:
        return configured
    groups = tuple(group for group in context.user_groups if str(group).strip())
    if not groups:
        raise PolicyError(
            "current_user_group_admin requires the requester to belong to a user group"
        )
    return f"group-admins:{groups[0]}"
