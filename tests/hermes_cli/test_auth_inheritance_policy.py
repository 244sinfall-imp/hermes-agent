"""Synthetic credentials only; no network, real homes, or account state."""
import json
import socket

import pytest

from hermes_cli import auth
from hermes_cli.auth_codex import resolve_codex_runtime_credentials
from hermes_cli.auth_policy import inherit_root_credentials_enabled


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / "root"
    profile = root / "profiles" / "business"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-cli"))
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network forbidden"))
    root_state = {"providers": {"openai-codex": {"tokens": {
        "access_token": "ROOT_SENTINEL", "refresh_token": "ROOT_REFRESH"}}},
        "credential_pool": {"openai-codex": [{"id": "root-row", "source": "device_code",
            "access_token": "ROOT_SENTINEL", "refresh_token": "ROOT_REFRESH"}]}}
    (root / "auth.json").write_text(json.dumps(root_state))
    (profile / "config.yaml").write_text(
        "auth:\n  inherit_root_credentials: false\n  adopt_external_logins: false\nfallback_providers: []\n")
    return root, profile


@pytest.mark.parametrize("state", [None, {}, {"tokens": {}}, {"tokens": []},
                                    {"tokens": {"id_token": "ID_ONLY"}}])
def test_missing_incomplete_invalid_never_borrow(homes, state):
    root, profile = homes
    before = (root / "auth.json").read_bytes()
    if state is not None:
        (profile / "auth.json").write_text(json.dumps({"providers": {"openai-codex": state}}))
    assert auth._global_auth_file_path() is None
    assert auth.read_credential_pool("openai-codex") == []
    assert auth.read_credential_pool() == {}
    for readonly in (True, False):
        with pytest.raises(auth.AuthError):
            resolve_codex_runtime_credentials(read_only=readonly)
    assert (root / "auth.json").read_bytes() == before


def test_corrupt_local_store_never_borrows(homes):
    _, profile = homes
    (profile / "auth.json").write_text("not json")
    with pytest.raises(auth.AuthError):
        resolve_codex_runtime_credentials(read_only=True)


@pytest.mark.parametrize("policy", ["", "auth:\n  inherit_root_credentials: true\n"])
def test_legacy_enabled(homes, policy):
    root, profile = homes
    (profile / "config.yaml").write_text(policy)
    assert inherit_root_credentials_enabled()
    assert auth._global_auth_file_path() == root / "auth.json"
    assert resolve_codex_runtime_credentials(read_only=True)["api_key"] == "ROOT_SENTINEL"
    assert auth.read_credential_pool("openai-codex")[0]["access_token"] == "ROOT_SENTINEL"
    assert auth.read_credential_pool()["openai-codex"][0]["access_token"] == "ROOT_SENTINEL"


def test_profile_singleton_and_pool(homes):
    _, _profile = homes
    auth._save_codex_tokens({"access_token": "WORK", "refresh_token": "WORK_REFRESH"})
    assert resolve_codex_runtime_credentials(read_only=True)["api_key"] == "WORK"
    auth._save_auth_store({"providers": {}, "credential_pool": {"openai-codex": [
        {"id": "work", "source": "device_code", "access_token": "WORK"}]}})
    assert resolve_codex_runtime_credentials(read_only=True)["api_key"] == "WORK"
    assert auth.read_credential_pool()["openai-codex"][0]["id"] == "work"


def test_expired_refresh_failure_no_recovery_to_root(homes, monkeypatch):
    root, _ = homes
    before = (root / "auth.json").read_bytes()
    auth._save_codex_tokens({"access_token": "EXPIRED_WORK", "refresh_token": "WORK_REFRESH"})
    monkeypatch.setattr(auth, "_codex_access_token_is_expiring", lambda *a: True)
    import hermes_cli.auth_codex as codex
    def fail_refresh(*a):
        raise auth.AuthError("revoked", code="invalid_grant", relogin_required=True)
    monkeypatch.setattr(codex, "_refresh_codex_auth_tokens", fail_refresh)
    with pytest.raises(auth.AuthError):
        resolve_codex_runtime_credentials()
    assert (root / "auth.json").read_bytes() == before


@pytest.mark.parametrize("value", ['"false"', "null", "[]", "1", '"true"'])
def test_invalid_policy_fails_closed(homes, value):
    _, profile = homes
    (profile / "config.yaml").write_text("auth:\n  inherit_root_credentials: " + value + "\n")
    assert not inherit_root_credentials_enabled()
    assert auth._load_global_auth_store() == {}
    from hermes_cli.config import validate_config_structure
    import hermes_yaml as yaml
    issues = validate_config_structure(yaml.safe_load((profile / "config.yaml").read_text()))
    assert any("inherit_root_credentials" in i.message for i in issues)


@pytest.mark.parametrize("text", ["auth: [", "[]", "auth: false", "auth: null"])
def test_broken_config_fails_closed(homes, text):
    _, profile = homes
    (profile / "config.yaml").write_text(text)
    assert not inherit_root_credentials_enabled()
    assert auth._global_auth_file_path() is None


def test_profile_switch_and_cached_root(homes, monkeypatch):
    root, profile = homes
    other = root / "profiles" / "legacy"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert auth._load_global_auth_store()["providers"]
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert auth._load_global_auth_store() == {}
    monkeypatch.setenv("HERMES_HOME", str(root))
    assert resolve_codex_runtime_credentials(read_only=True)["api_key"] == "ROOT_SENTINEL"


def test_root_read_write_lock_stale_targets_denied(homes):
    root, _ = homes
    path = root / "auth.json"
    before = path.read_bytes()
    with pytest.raises(PermissionError):
        auth._load_auth_store(path)
    with pytest.raises(PermissionError):
        auth._save_auth_store({}, target_path=path)
    with pytest.raises(PermissionError):
        with auth._auth_store_lock(target_path=path):
            pass
    with pytest.raises(PermissionError):
        auth._save_provider_state_to_source({}, "openai-codex", {}, path)
    from agent.credential_pool import _update_root_pool_rows
    with pytest.raises(PermissionError):
        _update_root_pool_rows("openai-codex", [], path)
    assert path.read_bytes() == before
    assert not path.with_suffix(".lock").exists()


def test_refresh_write_through_and_heal_are_disabled(homes):
    root, _ = homes
    before = (root / "auth.json").read_bytes()
    auth._save_codex_tokens({"access_token": "WORK", "refresh_token": "WORK_REFRESH"}, write_through=True)
    from agent.credential_pool import _write_through_provider_state_to_global_root
    _write_through_provider_state_to_global_root("openai-codex", {"tokens": {}})
    auth.heal_forked_single_use_oauth_grants("openai-codex")
    assert (root / "auth.json").read_bytes() == before


def test_anthropic_root_target_denied(homes):
    root, _ = homes
    from agent.anthropic_credentials import _root_hermes_oauth_file, _write_hermes_oauth_credentials
    assert _root_hermes_oauth_file() is None
    with pytest.raises(PermissionError):
        _write_hermes_oauth_credentials("WORK", "REFRESH", 1, target=root / ".anthropic_oauth.json")


def test_nous_shared_store_is_profile_local(homes, monkeypatch):
    root, profile = homes
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(root / "shared"))
    from hermes_cli.auth_nous import _nous_shared_auth_dir
    assert _nous_shared_auth_dir() == profile / "shared"


def test_context_local_profile_switch(homes):
    root, _profile = homes
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(str(root))
    try:
        assert auth._load_provider_state(auth._load_auth_store(), "openai-codex")["tokens"]["access_token"] == "ROOT_SENTINEL"
    finally:
        reset_hermes_home_override(token)
    assert auth._global_auth_file_path() is None
    assert auth._load_provider_state(auth._load_auth_store(), "openai-codex") is None


def test_pool_load_and_removal_never_touch_root(homes):
    root, _ = homes
    before = (root / "auth.json").read_bytes()
    from agent.credential_pool import load_pool
    pool = load_pool("openai-codex")
    assert not pool._entries
    pool._persist(removed_ids=["root-row"])
    assert (root / "auth.json").read_bytes() == before


def test_unreadable_config_fails_closed(homes, monkeypatch):
    from hermes_cli import config
    def unreadable(*args):
        raise PermissionError("fixture unreadable")
    monkeypatch.setattr(config, "require_readable_config_before_write", unreadable)
    assert not inherit_root_credentials_enabled()
    assert auth._load_global_auth_store() == {}


def test_symlinked_local_store_denied(homes):
    root, profile = homes
    (profile / "auth.json").symlink_to(root / "auth.json")
    with pytest.raises(PermissionError):
        auth._load_auth_store()
