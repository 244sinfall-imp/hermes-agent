"""Per-job enabled_tools / disabled_tools: persisted, updatable, and handed to the cron agent."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import importlib

    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    return home


def test_create_persists_lists_and_old_jobs_stay_byte_identical(hermes_env):
    from cron.jobs import create_job, get_job
    plain = create_job(prompt="digest", schedule="every 1h")
    assert "enabled_tools" not in plain and "disabled_tools" not in plain

    job = create_job(prompt="triage", schedule="every 1h", enabled_tools=["read_file", " read_file "],
                     disabled_tools=["mcp_slack_slack_send_message"])
    stored = get_job(job["id"])
    assert stored["enabled_tools"] == ["read_file"]
    assert stored["disabled_tools"] == ["mcp_slack_slack_send_message"]


def test_explicit_empty_enabled_tools_persists(hermes_env):
    from cron.jobs import create_job, get_job
    job = create_job(prompt="reminder", schedule="every 1h", enabled_tools=[])
    assert get_job(job["id"])["enabled_tools"] == []


def test_non_list_rejected_before_store(hermes_env):
    from cron.jobs import create_job, list_jobs
    with pytest.raises(ValueError, match="enabled_tools"):
        create_job(prompt="x", schedule="every 1h", enabled_tools="terminal")
    assert list_jobs(include_disabled=True) == []


def test_update_sets_and_clears(hermes_env):
    from cron.jobs import create_job, update_job
    job = create_job(prompt="x", schedule="every 1h")
    updated = update_job(job["id"], {"disabled_tools": ["terminal"]})
    assert updated["disabled_tools"] == ["terminal"]
    updated = update_job(job["id"], {"disabled_tools": []})
    assert updated["disabled_tools"] == []


def test_cronjob_tool_roundtrip(hermes_env):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob
    res = json.loads(cronjob(action="create", prompt="triage", schedule="every 1h",
                             disabled_tools=["mcp_newo_slack_slack_send_message"]))
    assert res["success"] is True, res
    assert get_job(res["job_id"])["disabled_tools"] == ["mcp_newo_slack_slack_send_message"]
    res2 = json.loads(cronjob(action="update", job_id=res["job_id"], enabled_tools=["read_file"]))
    assert res2["success"] is True, res2
    assert get_job(res["job_id"])["enabled_tools"] == ["read_file"]


def test_schema_exposes_fields():
    from tools.cronjob_tools import CRONJOB_SCHEMA, _HANDLER_FORWARDED_ARGS
    props = CRONJOB_SCHEMA["parameters"]["properties"]
    assert "enabled_tools" in props and "disabled_tools" in props
    assert "enabled_tools" in _HANDLER_FORWARDED_ARGS and "disabled_tools" in _HANDLER_FORWARDED_ARGS


def test_cron_agent_receives_policy(hermes_env):
    import cron.scheduler as sched
    captured = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured.update(kw)

    setup = sched._CronAgentSetup() if hasattr(sched, "_CronAgentSetup") else None
    if setup is None:
        pytest.skip("installed scheduler has no _CronAgentSetup")
    setup.runtime, setup.model = {}, "m"
    for attr in ("max_iterations", "reasoning_config", "prefill_messages", "fallback_model", "credential_pool"):
        if not hasattr(setup, attr):
            setattr(setup, attr, None)
    job = {"id": "j", "enabled_toolsets": ["file"], "enabled_tools": ["read_file"],
           "disabled_tools": ["write_file"]}
    sched._construct_cron_agent(FakeAgent, job, {}, setup, workdir=None, session_id="s", session_db=None)
    assert captured["enabled_tools"] == ["read_file"]
    assert captured["disabled_tools"] == ["write_file"]


def test_job_definition_fields_include_policy():
    from cron.job_definition import JOB_DEFINITION_FIELDS
    assert {"enabled_tools", "disabled_tools"} <= JOB_DEFINITION_FIELDS
