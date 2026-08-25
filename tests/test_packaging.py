"""The repo as something someone else installs.

These are cheap and catch the failures that only show up on a machine that is
not this one: a documented setting that no longer exists, a dependency nobody
imports, a declared file that was never written.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Set by the operating system, not by us.
OS_VARS = {"LOCALAPPDATA", "XDG_DATA_HOME"}
# Accepted as an alias for WA_AUTH_TOKEN; documented in a comment, not a key.
ALIASES = {"MCP_AUTH_TOKEN"}


def _env_read_by_config() -> set[str]:
    src = (ROOT / "wa_mcp" / "config.py").read_text()
    return set(re.findall(r'(?:getenv|_flag)\(\s*"([A-Z_]+)"', src)) - OS_VARS - ALIASES


def _env_documented() -> set[str]:
    return set(re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))


def test_every_setting_is_documented():
    """An undocumented variable is one nobody deploying will ever set."""
    missing = sorted(_env_read_by_config() - _env_documented())
    assert not missing, f".env.example does not mention: {missing}"


def test_nothing_documented_has_been_removed():
    """Worse than undocumented: a setting that silently does nothing."""
    stale = sorted(_env_documented() - _env_read_by_config())
    assert not stale, f".env.example documents settings nothing reads: {stale}"


def test_declared_dependencies_are_actually_imported():
    """A dependency nobody imports is weight in everyone's install."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    declared = re.findall(r'"([A-Za-z0-9_.-]+)', block)

    src = "\n".join(p.read_text() for p in (ROOT / "wa_mcp").rglob("*.py"))
    # import name != distribution name for these.
    alias = {"python-dotenv": "dotenv", "uvicorn[standard]": "uvicorn",
             "SQLAlchemy[asyncio]": "sqlalchemy"}
    unused = []
    for dist in declared:
        mod = alias.get(dist, dist).split("[")[0].lower()
        if not re.search(rf"\b(?:import|from)\s+{re.escape(mod)}\b", src):
            unused.append(dist)
    assert not unused, f"declared but never imported: {unused}"


@pytest.mark.parametrize("name", ["README.md", "LICENSE", ".env.example",
                                  "Dockerfile", ".dockerignore", ".gitignore"])
def test_the_files_a_deployment_needs_exist(name):
    p = ROOT / name
    assert p.is_file() and p.stat().st_size > 0, f"{name} is missing or empty"


def test_pyproject_readme_points_at_a_real_file():
    """Declared and absent, the build still succeeds — and ships no description."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'readme\s*=\s*"([^"]+)"', pyproject)
    assert m and (ROOT / m.group(1)).is_file()


def test_the_dockerfile_installs_libmagic():
    """Without it neonize fails to import, with an error naming Python."""
    assert "libmagic" in (ROOT / "Dockerfile").read_text()


def test_the_dockerfile_persists_the_session():
    """History syncs once, at pair time. An unmounted session means re-pairing."""
    df = (ROOT / "Dockerfile").read_text()
    assert "VOLUME" in df and "WA_DATA_DIR" in df
