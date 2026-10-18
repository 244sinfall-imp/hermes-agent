"""Preserve native Claude metadata when rotating only the managed token fields.

Regression for NousResearch/hermes-agent#83338. Synthetic files only; no refresh
request, native CLI or real credential store is used.
"""
import json
import stat

import pytest

from agent import anthropic_credentials as credentials


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "selected" / ".credentials.json"
    path.parent.mkdir()
    other = tmp_path / "other-owner.json"
    other.write_text('{"owner":"OTHER_SYNTHETIC_OWNER"}')
    mirror_calls = []
    monkeypatch.setattr(credentials, "claude_code_credentials_path", lambda: path)
    monkeypatch.setattr(
        credentials, "_mirror_claude_code_credentials_to_keychain",
        lambda *args, **kwargs: mirror_calls.append((args, kwargs)),
    )
    return path, other, mirror_calls


@pytest.mark.parametrize("plan", ["pro", "max", None, "future-tier"])
@pytest.mark.parametrize("scopes", [None, [], ["new-synthetic-scope"]])
def test_preserves_nested_metadata_and_updates_only_managed_fields(store, plan, scopes):
    path, other, mirror_calls = store
    oauth = {
        "accessToken": "old-synthetic-access", "refreshToken": "old-synthetic-refresh",
        "expiresAt": 1, "scopes": ["old-synthetic-scope"],
        "subscriptionType": plan, "rateLimitTier": "synthetic-tier",
        "refreshTokenExpiresAt": 999, "futureMetadata": {"owner": "SELECTED_SYNTHETIC_OWNER"},
    }
    old = {"claudeAiOauth": oauth, "unrelatedTopLevel": {"keep": True}}
    path.write_text(json.dumps(old))
    other_before = other.read_bytes()

    credentials._write_claude_code_credentials(
        "new-synthetic-access", "new-synthetic-refresh", 2, scopes=scopes,
    )

    expected = dict(oauth, accessToken="new-synthetic-access", refreshToken="new-synthetic-refresh", expiresAt=2)
    if scopes is not None:
        expected["scopes"] = scopes
    assert json.loads(path.read_text()) == dict(old, claudeAiOauth=expected)
    assert other.read_bytes() == other_before
    assert len(mirror_calls) == 1


@pytest.mark.parametrize("existing", [{}, {"unrelatedTopLevel": "keep"},
                                     {"claudeAiOauth": None}, {"claudeAiOauth": []},
                                     {"claudeAiOauth": "invalid"}, {"claudeAiOauth": 1}])
def test_missing_or_nonobject_nested_record_gets_only_new_managed_fields(store, existing):
    path, other, _ = store
    path.write_text(json.dumps(existing))
    other_before = other.read_bytes()
    credentials._write_claude_code_credentials("new-access", "new-refresh", 2)
    assert json.loads(path.read_text()) == dict(existing, claudeAiOauth={
        "accessToken": "new-access", "refreshToken": "new-refresh", "expiresAt": 2,
    })
    assert other.read_bytes() == other_before


@pytest.mark.platforms("posix")
def test_rotated_file_remains_private(store):
    path, _, _ = store
    credentials._write_claude_code_credentials("new-access", "new-refresh", 2)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_json_is_not_replaced(store):
    path, _, mirror_calls = store
    path.write_text("not-json")
    before = path.read_bytes()
    with pytest.raises(credentials.CredentialPersistError):
        credentials._write_claude_code_credentials("new-access", "new-refresh", 2)
    assert path.read_bytes() == before
    assert not mirror_calls


def test_failed_private_commit_preserves_old_record(store, monkeypatch):
    path, _, mirror_calls = store
    path.write_text('{"claudeAiOauth":{"subscriptionType":"pro"}}')
    before = path.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("synthetic persist failure")

    monkeypatch.setattr(credentials, "_atomic_write_private_json", fail_write)
    with pytest.raises(credentials.CredentialPersistError):
        credentials._write_claude_code_credentials("new-access", "new-refresh", 2)
    assert path.read_bytes() == before
    assert not mirror_calls
