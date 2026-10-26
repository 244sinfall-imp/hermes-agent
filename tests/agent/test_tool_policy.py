"""Per-tool allow/deny policy (enabled_tools / disabled_tools).

Toolsets select bundles; the per-tool policy narrows to single names and is enforced at EXECUTION,
not only by hiding schemas: a hidden tool must stay unreachable through the agent loop, the
tool_call bridge, nested handle_function_call (execute_code RPC, connector batches), direct
registry dispatch (plugin dispatch_tool), and agents constructed inside a restricted turn.
"""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)


# --- policy semantics ----------------------------------------------------------------------------

def test_unset_policy_is_unrestricted():
    from agent.tool_policy import UNRESTRICTED, ToolPolicy
    policy = ToolPolicy.from_lists(None, None)
    assert policy is UNRESTRICTED and not policy.restrictive
    assert policy.permits("terminal")


def test_allowlist_exact_and_glob():
    from agent.tool_policy import ToolPolicy
    policy = ToolPolicy.from_lists(["read_file", "mcp_slack_*_read*"], None)
    assert policy.permits("read_file")
    assert policy.permits("mcp_slack_slack_read_thread")
    assert not policy.permits("mcp_slack_slack_send_message")
    assert not policy.permits("terminal")


def test_deny_wins_over_allow():
    from agent.tool_policy import ToolPolicy
    policy = ToolPolicy.from_lists(["mcp_slack_*"], ["mcp_slack_slack_send_message"])
    assert policy.permits("mcp_slack_slack_read_thread")
    assert not policy.permits("mcp_slack_slack_send_message")


def test_explicit_empty_allowlist_permits_nothing_including_bridge():
    from agent.tool_policy import ToolPolicy
    policy = ToolPolicy.from_lists([], None)
    assert policy.restrictive
    for name in ("read_file", "terminal", "tool_call", "tool_search", "tool_describe"):
        assert not policy.permits(name)


def test_bridge_rides_along_with_nonempty_allowlist_unless_denied():
    from agent.tool_policy import ToolPolicy
    assert ToolPolicy.from_lists(["read_file"], None).permits("tool_call")
    assert not ToolPolicy.from_lists(["read_file"], ["tool_call"]).permits("tool_call")


def test_unknown_names_grant_nothing():
    from agent.tool_policy import ToolPolicy
    policy = ToolPolicy.from_lists(["no_such_tool"], None)
    assert not policy.permits("terminal")


def test_non_list_input_is_rejected():
    from agent.tool_policy import ToolPolicy
    with pytest.raises(ValueError):
        ToolPolicy.from_lists("terminal", None)


def test_intersection_never_widens_and_nested_binding_narrows():
    from agent.tool_policy import ToolPolicy, current_policy
    outer = ToolPolicy.from_lists(["read_file", "terminal"], None)
    inner = ToolPolicy.from_lists(["terminal", "write_file"], None)
    both = outer.intersect(inner)
    assert both.permits("terminal")
    assert not both.permits("read_file") and not both.permits("write_file")
    with outer.bound():
        # An unrestricted inner binding keeps the outer narrowing.
        with ToolPolicy.from_lists(None, None).bound():
            assert not current_policy().permits("write_file")
        with inner.bound():
            assert current_policy().permits("terminal")
            assert not current_policy().permits("read_file")
    assert not current_policy().restrictive


def test_bound_policy_propagates_to_threads_that_copy_context():
    from agent.tool_policy import ToolPolicy, current_policy
    from tools.thread_context import propagate_context_to_thread
    seen = {}

    def worker():
        seen["read_file"] = current_policy().permits("read_file")
        seen["terminal"] = current_policy().permits("terminal")

    with ToolPolicy.from_lists(["read_file"], None).bound():
        t = threading.Thread(target=propagate_context_to_thread(worker))
        t.start()
        t.join(5)
    assert seen == {"read_file": True, "terminal": False}


def test_filter_defs_and_names():
    from agent.tool_policy import ToolPolicy
    policy = ToolPolicy.from_lists(None, ["terminal"])
    defs = [{"type": "function", "function": {"name": n}} for n in ("terminal", "read_file")]
    assert [d["function"]["name"] for d in policy.filter_defs(defs)] == ["read_file"]
    assert policy.filter_names({"terminal", "read_file"}) == {"read_file"}


# --- execution chokepoints ------------------------------------------------------------------------

def _stub_agent(policy):
    import run_agent as _ra

    class _Stub:
        _interrupt_requested = False
        _interrupt_message = None
        log_prefix = ""
        quiet_mode = True
        verbose_logging = False
        log_prefix_chars = 200
        _checkpoint_mgr = MagicMock(enabled=False)
        tool_progress_callback = None
        tool_start_callback = None
        tool_complete_callback = None
        tool_progress_mode = "off"
        _todo_store = MagicMock()
        _session_db = None
        valid_tool_names = set()
        _turns_since_memory = 0
        _iters_since_skill = 0
        _current_tool = None
        _last_activity = 0.0
        session_id = ""
        _current_turn_id = ""
        _current_api_request_id = ""

        def __init__(self):
            self._tool_worker_threads: set = set()
            self._tool_worker_threads_lock = threading.Lock()
            self._active_children_lock = threading.Lock()

        def _touch_activity(self, desc):
            self._last_activity = time.time()

        def _vprint(self, msg, force=False):
            pass

        def _safe_print(self, msg):
            pass

        def _should_emit_quiet_tool_messages(self):
            return False

        def _should_start_quiet_spinner(self):
            return False

        def _has_stream_consumers(self):
            return False

    stub = _Stub()
    stub.interrupt = _ra.AIAgent.interrupt.__get__(stub)
    stub.clear_interrupt = _ra.AIAgent.clear_interrupt.__get__(stub)
    stub._guardrail_block_result = lambda d: json.dumps({"error": "blocked"})
    stub._tool_guardrails = MagicMock(before_call=lambda name, args: MagicMock(allows_execution=True))
    stub._tool_policy = policy
    return stub


def _run_managed(agent, name, execute):
    import agent.tool_executor as te
    return te._run_agent_tool_execution_middleware(
        agent, function_name=name, function_args={}, effective_task_id="task",
        tool_call_id="tc1", execute=execute, display_index=1,
    )


def test_agent_loop_blocks_denied_tool_without_executing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    from agent.tool_policy import TOOL_POLICY_ERROR_TYPE, ToolPolicy
    agent = _stub_agent(ToolPolicy.from_lists(None, ["terminal"]))
    calls = []
    result = _run_managed(agent, "terminal", lambda args: calls.append(args) or "{}")
    assert result.blocked and not calls
    assert "per-tool policy" in json.loads(result.result)["error"]
    assert TOOL_POLICY_ERROR_TYPE  # error type exported for observers


def test_agent_loop_runs_allowed_tool_and_binds_policy_for_nested_calls(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    from agent.tool_policy import ToolPolicy, current_policy
    agent = _stub_agent(ToolPolicy.from_lists(["execute_code"], None))
    seen = {}

    def execute(args):
        policy = current_policy()
        seen["nested_terminal"] = policy.permits("terminal")
        seen["nested_self"] = policy.permits("execute_code")
        return json.dumps({"ok": True})

    result = _run_managed(agent, "execute_code", execute)
    assert json.loads(result.result) == {"ok": True}
    assert seen == {"nested_terminal": False, "nested_self": True}
    assert not current_policy().restrictive  # unbound after the call


def test_unrestricted_agent_behaviour_unchanged(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    from agent.tool_policy import UNRESTRICTED
    agent = _stub_agent(UNRESTRICTED)
    result = _run_managed(agent, "terminal", lambda args: json.dumps({"ok": True}))
    assert json.loads(result.result) == {"ok": True}


def test_handle_function_call_blocks_under_bound_policy():
    import model_tools
    from agent.tool_policy import ToolPolicy
    from tools.registry import registry
    calls = []
    registry.register(name="policy_probe_secret", toolset="policy_probe",
                      schema={"name": "policy_probe_secret", "description": "x",
                              "parameters": {"type": "object", "properties": {}}},
                      handler=lambda args, **kw: calls.append(1) or json.dumps({"executed": True}))
    try:
        with ToolPolicy.from_lists(None, ["policy_probe_secret"]).bound():
            out = json.loads(model_tools.handle_function_call("policy_probe_secret", {}))
        assert "per-tool policy" in out["error"] and not calls
        # Same call outside the restricted turn still works (no global state leak).
        assert json.loads(model_tools.handle_function_call("policy_probe_secret", {})) == {"executed": True}
    finally:
        registry.deregister("policy_probe_secret")


def test_tool_call_bridge_cannot_reach_denied_tool():
    import model_tools
    from agent.tool_policy import ToolPolicy
    from tools.registry import registry
    calls = []
    registry.register(name="policy_probe_hidden", toolset="policy_probe",
                      schema={"name": "policy_probe_hidden", "description": "x",
                              "parameters": {"type": "object", "properties": {}}},
                      handler=lambda args, **kw: calls.append(1) or json.dumps({"executed": True}))
    try:
        with ToolPolicy.from_lists(None, ["policy_probe_hidden"]).bound():
            out = model_tools.handle_function_call(
                "tool_call", {"name": "policy_probe_hidden", "arguments": {}}, enabled_toolsets=None)
        assert "error" in json.loads(out) and not calls
    finally:
        registry.deregister("policy_probe_hidden")


def test_direct_registry_dispatch_is_guarded():
    from agent.tool_policy import ToolPolicy
    from tools.registry import registry
    calls = []
    registry.register(name="policy_probe_direct", toolset="policy_probe",
                      schema={"name": "policy_probe_direct", "description": "x",
                              "parameters": {"type": "object", "properties": {}}},
                      handler=lambda args, **kw: calls.append(1) or json.dumps({"executed": True}))
    try:
        with ToolPolicy.from_lists(["read_file"], None).bound():
            out = registry.dispatch("policy_probe_direct", {})
        assert "per-tool policy" in json.loads(out)["error"] and not calls
    finally:
        registry.deregister("policy_probe_direct")


def test_execute_code_sandbox_respects_policy_and_never_widens_on_empty():
    from agent.tool_policy import ToolPolicy
    from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, _sandbox_tools_for
    assert _sandbox_tools_for(None) == SANDBOX_ALLOWED_TOOLS
    with ToolPolicy.from_lists(None, ["terminal", "write_file", "patch"]).bound():
        tools = _sandbox_tools_for(None)
    assert "terminal" not in tools and "write_file" not in tools and "read_file" in tools
    # A policy that forbids every sandbox tool must yield NONE, not fall back to all of them.
    with ToolPolicy.from_lists(["vision_analyze"], None).bound():
        assert _sandbox_tools_for(["terminal"]) == frozenset()


# --- agent construction ---------------------------------------------------------------------------

def _build_agent(monkeypatch, **kwargs):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    from run_agent import AIAgent
    return AIAgent(
        model="test/model", api_key="synthetic", base_url="http://127.0.0.1:9/v1", provider="custom",
        api_mode="chat_completions", quiet_mode=True, skip_memory=True, skip_context_files=True,
        enabled_toolsets=["file", "terminal"], **kwargs,
    )


def test_agent_schemas_follow_policy(monkeypatch):
    agent = _build_agent(monkeypatch, disabled_tools=["write_file", "patch"])
    names = {t["function"]["name"] for t in agent.tools}
    assert "read_file" in names and "write_file" not in names and "patch" not in names
    assert agent.valid_tool_names == names


def test_agent_without_policy_is_unchanged(monkeypatch):
    plain = _build_agent(monkeypatch)
    assert {"read_file", "write_file", "terminal"} <= plain.valid_tool_names
    assert not plain._tool_policy.restrictive


def test_agent_built_inside_restricted_turn_inherits_policy(monkeypatch):
    """Delegation children are constructed inside the parent's delegate_task execution."""
    from agent.tool_policy import ToolPolicy
    with ToolPolicy.from_lists(None, ["terminal"]).bound():
        child = _build_agent(monkeypatch, enabled_tools=["terminal", "read_file"])
    assert "terminal" not in child.valid_tool_names
    assert child.valid_tool_names <= {"read_file", "tool_search", "tool_describe", "tool_call"}
    assert not child._tool_policy.permits("terminal")


def test_codex_app_server_refuses_restrictive_policy():
    from agent.agent_init import _refuse_tool_policy_on_codex_app_server
    from agent.tool_policy import UNRESTRICTED, ToolPolicy
    agent = MagicMock(api_mode="codex_app_server", _tool_policy=ToolPolicy.from_lists(None, ["terminal"]))
    with pytest.raises(RuntimeError, match="codex_app_server"):
        _refuse_tool_policy_on_codex_app_server(agent)
    _refuse_tool_policy_on_codex_app_server(MagicMock(api_mode="codex_app_server", _tool_policy=UNRESTRICTED))
    _refuse_tool_policy_on_codex_app_server(
        MagicMock(api_mode="chat_completions", _tool_policy=ToolPolicy.from_lists(None, ["terminal"])))


def test_mcp_refresh_keeps_policy(monkeypatch):
    from agent.tool_policy import ToolPolicy
    from tools.mcp_tool_agent import _drop_session_tools
    agent = MagicMock(platform="cli", _tool_policy=ToolPolicy.from_lists(None, ["mcp_x_send"]))
    monkeypatch.setattr("toolsets.agent_tool_drops", lambda a: frozenset())
    defs = [{"type": "function", "function": {"name": n}} for n in ("mcp_x_send", "mcp_x_read")]
    out_defs, out_names = _drop_session_tools(agent, defs, {"mcp_x_send", "mcp_x_read"})
    assert out_names == {"mcp_x_read"} and [d["function"]["name"] for d in out_defs] == ["mcp_x_read"]


def test_review_fork_inherits_parent_policy():
    """/btw and background-review forks are built after the turn, outside any bound policy."""
    from types import SimpleNamespace
    from agent.background_review import _inherit_parent_tool_surface
    from agent.tool_policy import UNRESTRICTED, ToolPolicy
    parent = SimpleNamespace(tools=[{"type": "function", "function": {"name": "read_file"}}],
                             _tool_policy=ToolPolicy.from_lists(None, ["terminal"]))
    fork = SimpleNamespace(_tool_policy=UNRESTRICTED)
    _inherit_parent_tool_surface(fork, parent)
    assert not fork._tool_policy.permits("terminal")
    assert fork._tool_policy.permits("read_file")
