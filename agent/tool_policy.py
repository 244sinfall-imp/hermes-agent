"""Per-tool allow/deny policy layered on top of toolset selection.

Toolsets choose *bundles*; this narrows a session to individual tool names. A policy is a
stack of layers, every layer must permit a name:

* ``allow`` — ``None`` (no per-tool allowlist) or a frozenset of exact names / fnmatch globs.
  An explicit EMPTY allowlist permits nothing (mirrors ``enabled_toolsets=[]``, #82010).
* ``deny`` — names/globs that are never permitted. Deny wins over allow in the same layer.

Unknown names in an allowlist grant nothing (fail closed); a policy can only ever narrow what
toolsets already selected, never widen it.

Enforcement is by name at EXECUTION time, not only by schema filtering: hiding a schema does not
stop ``tool_call``, ``execute_code`` RPC, connector batches or plugin ``dispatch_tool`` from
reaching the registry. The active policy rides a ContextVar so worker threads (which copy the
context) and agents constructed inside a tool call (delegation children) inherit it; a nested
binding is always the INTERSECTION with the outer one, so it cannot widen.

Not a sandbox: a permitted ``terminal``/``execute_code`` can still do anything the OS user can.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Iterable, Iterator, Optional

# tool_search / tool_describe / tool_call: catalog + dispatch bridge. The bridge itself is
# permitted whenever anything at all is allowed; the UNDERLYING tool is checked on re-dispatch.
_BRIDGE_TOOL_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})
_GLOB_CHARS = frozenset("*?[")

TOOL_POLICY_ERROR_TYPE = "tool_policy_block"


def _normalize_names(value: Any, field: str) -> Optional[frozenset]:
    """None stays None; a list/tuple/set becomes a frozenset of non-blank stripped strings."""
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{field} must be a list of tool names (or [] for none).")
    return frozenset(s for s in (str(v).strip() for v in value) if s)


def _matches(name: str, patterns: frozenset) -> bool:
    if name in patterns:
        return True
    return any(_GLOB_CHARS & set(p) and fnmatchcase(name, p) for p in patterns)


@dataclass(frozen=True)
class _Layer:
    allow: Optional[frozenset]
    deny: frozenset

    def permits(self, name: str) -> bool:
        if _matches(name, self.deny):
            return False
        if self.allow is None:
            return True
        if name in _BRIDGE_TOOL_NAMES:
            return bool(self.allow)  # bridge rides along unless the allowlist is explicitly empty
        return _matches(name, self.allow)

    def as_dict(self) -> dict:
        return {"allow": None if self.allow is None else sorted(self.allow), "deny": sorted(self.deny)}


@dataclass(frozen=True)
class ToolPolicy:
    layers: tuple = ()

    @classmethod
    def from_lists(cls, enabled_tools: Any = None, disabled_tools: Any = None) -> ToolPolicy:
        allow = _normalize_names(enabled_tools, "enabled_tools")
        deny = _normalize_names(disabled_tools, "disabled_tools") or frozenset()
        if allow is None and not deny:
            return UNRESTRICTED
        return cls((_Layer(allow, deny),))

    @property
    def restrictive(self) -> bool:
        return bool(self.layers)

    def permits(self, name: str) -> bool:
        return all(layer.permits(name) for layer in self.layers)

    def intersect(self, other: Optional[ToolPolicy]) -> ToolPolicy:
        if other is None or not other.layers:
            return self
        if not self.layers:
            return other
        seen, merged = set(), []
        for layer in self.layers + other.layers:
            if layer not in seen:
                seen.add(layer)
                merged.append(layer)
        return ToolPolicy(tuple(merged))

    def filter_names(self, names: Iterable[str]) -> set:
        return {n for n in names if self.permits(n)} if self.layers else set(names)

    def filter_defs(self, defs: Optional[list]) -> list:
        if not self.layers:
            return list(defs or [])
        return [d for d in (defs or []) if self.permits(_def_name(d))]

    def fingerprint(self) -> Optional[tuple]:
        return tuple(self.layers) or None

    def describe(self) -> list:
        return [layer.as_dict() for layer in self.layers]

    @contextlib.contextmanager
    def bound(self) -> Iterator[ToolPolicy]:
        """Bind this policy (intersected with any outer one) for the block."""
        effective = self.intersect(_CURRENT.get())
        token = _CURRENT.set(effective)
        try:
            yield effective
        finally:
            _CURRENT.reset(token)


def _def_name(tool_def: Any) -> str:
    if not isinstance(tool_def, dict):
        return ""
    fn = tool_def.get("function")
    if not isinstance(fn, dict):
        fn = tool_def
    return str(fn.get("name") or "")


UNRESTRICTED = ToolPolicy(())

_CURRENT: contextvars.ContextVar[ToolPolicy] = contextvars.ContextVar("hermes_tool_policy", default=UNRESTRICTED)


def current_policy() -> ToolPolicy:
    return _CURRENT.get()


def agent_policy(agent: Any) -> ToolPolicy:
    policy = getattr(agent, "_tool_policy", None)
    return policy if isinstance(policy, ToolPolicy) else UNRESTRICTED


def denied_message(name: str) -> str:
    return (f"Tool '{name}' is not permitted in this session by its per-tool policy "
            "(enabled_tools/disabled_tools). Do not retry it through another tool.")


def denied_result(name: str) -> str:
    return json.dumps({"error": denied_message(name), "error_type": TOOL_POLICY_ERROR_TYPE}, ensure_ascii=False)


__all__ = [
    "TOOL_POLICY_ERROR_TYPE",
    "UNRESTRICTED",
    "ToolPolicy",
    "agent_policy",
    "current_policy",
    "denied_message",
    "denied_result",
]
