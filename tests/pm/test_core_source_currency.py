"""Core currency must describe the source snapshot, not just dependencies."""
from pathlib import Path

import pytest

from pm.packages import Venv
from pm.workspace import _copy_core_inputs


@pytest.fixture
def core(tmp_path):
    root = tmp_path / "core"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="core"\nversion="1"\n[tool.setuptools.packages.find]\ninclude=["hermes_cli*"]\n')
    (root / "uv.lock").write_text("version = 1\n")
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli/auth.py").write_text("policy = False\n")
    return root


def stamp(core):
    return Venv(core).expected_stamp([], plugin_dirs=[])


@pytest.mark.parametrize("change", ["edit", "add", "delete", "rename", "asset"])
def test_currency_changes_with_copied_source(core, change):
    before = stamp(core)
    auth = core / "hermes_cli/auth.py"
    if change == "edit":
        auth.write_text("policy = True\n")
    elif change == "add":
        (auth.parent / "auth_policy.py").write_text("enabled = True\n")
    elif change == "delete":
        auth.unlink()
    elif change == "rename":
        auth.rename(auth.with_name("renamed.py"))
    else:
        (auth.parent / "dist").mkdir()
        (auth.parent / "dist/index.html").write_text("asset")
    assert stamp(core) != before


@pytest.mark.parametrize("relative", ["tests/test_auth.py", "private/auth.json", ".env",
    "hermes_cli/__pycache__/auth.pyc", "hermes_cli/.private", "hermes_cli/build/file"])
def test_currency_ignores_uncopied_inputs(core, relative):
    before = stamp(core)
    target = core / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ignored")
    assert stamp(core) == before


def test_currency_ignores_symlinks_and_source_location(core, tmp_path):
    import shutil
    before = stamp(core)
    (core / "hermes_cli/link.py").symlink_to(core / "hermes_cli/auth.py")
    assert stamp(core) == before
    other = tmp_path / "other"
    shutil.copytree(core, other, symlinks=True)
    assert stamp(other) == before


def test_stamp_files_match_copier(core, tmp_path):
    from pm.workspace import core_sources_stamp
    destination = tmp_path / "snapshot"
    destination.mkdir()
    _copy_core_inputs(core, destination)
    assert core_sources_stamp(core) == core_sources_stamp(destination)
