from __future__ import annotations

from pathlib import Path
from typing import Any
from fnmatch import fnmatchcase

import yaml


class PolicyError(RuntimeError):
    pass


def load_yaml(path: str | Path) -> dict[str, Any]:
    policy_path = Path(path)
    if not policy_path.is_file():
        raise PolicyError(f"Governance policy not found: {policy_path}")
    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PolicyError(f"Invalid governance policy {policy_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PolicyError(f"Governance policy must be a mapping: {policy_path}")
    return loaded


class PolicyRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def policy_for(self, tool_name: str) -> dict[str, Any]:
        document = load_yaml(self.path)
        defaults = document.get("defaults") or {}
        tools = document.get("tools") or {}
        patterns = document.get("patterns") or []
        if (
            not isinstance(defaults, dict)
            or not isinstance(tools, dict)
            or not isinstance(patterns, list)
        ):
            raise PolicyError("tool-governance defaults/tools must be mappings")
        policy = dict(defaults)
        configured = tools.get(tool_name)
        if configured is None:
            for candidate in patterns:
                if not isinstance(candidate, dict) or not candidate.get("match"):
                    raise PolicyError("each tool policy pattern needs a match value")
                if fnmatchcase(tool_name, str(candidate["match"])):
                    configured = {
                        key: value for key, value in candidate.items() if key != "match"
                    }
                    break
        if configured is not None:
            if not isinstance(configured, dict):
                raise PolicyError(f"Policy for {tool_name!r} must be a mapping")
            policy.update(configured)
        policy["version"] = str(document.get("version", "unknown"))
        return policy
