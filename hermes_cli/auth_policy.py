"""Profile-local policy for implicit root credential inheritance.

Read the active home's raw config, not merged defaults or environment expansions:
malformed/unreadable policy must never turn credential inheritance back on. No
process-wide cache: gateway profile scopes can switch within one process.
"""
from pathlib import Path

from hermes_constants import get_default_hermes_root, get_hermes_home


def inherit_root_credentials_enabled() -> bool:
    from hermes_cli.config import require_readable_config_before_write

    try:
        config = require_readable_config_before_write(get_hermes_home() / "config.yaml")
        auth = config.get("auth", {})
        return isinstance(auth, dict) and auth.get("inherit_root_credentials", True) is True
    except Exception:
        return False


def guard_root_credential_path(path: Path) -> None:
    """Reject root-store access, including stale targets and linked profile stores.

    This is a credential resolver boundary, not a filesystem sandbox. Explicit
    environment keys, external CLI adoption and provider routing are independent.
    """
    root = get_default_hermes_root()
    if get_hermes_home().resolve() == root.resolve() or inherit_root_credentials_enabled():
        return
    for name in ("auth.json", ".anthropic_oauth.json"):
        target = root / name
        same = path.resolve() == target.resolve()
        try:
            same = same or path.samefile(target)
        except OSError:
            pass
        if same:
            raise PermissionError("auth.inherit_root_credentials is false: root credential access denied")
