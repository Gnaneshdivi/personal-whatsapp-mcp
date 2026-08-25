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
                                  "Dockerfile", ".dockerignore", ".gitignore",
                                  "CHANGELOG.md", "CONTRIBUTING.md",
                                  "SECURITY.md", ".github/workflows/ci.yml"])
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


# --------------------------------------------------------------- the docs

DOCS = ["setup.md", "settings.md", "auto-reply.md",
        "recipes.md", "architecture.md"]


@pytest.mark.parametrize("name", DOCS)
def test_the_docs_exist(name):
    p = ROOT / "docs" / name
    assert p.is_file() and p.stat().st_size > 500


def test_every_auto_reply_setting_is_documented():
    """64 fields is more than anyone will notice one missing from.

    A setting that exists but is written down nowhere is one nobody will ever
    set on purpose.
    """
    import dataclasses
    import sys

    sys.path.insert(0, str(ROOT))
    from wa_mcp.trigger.settings import TriggerSettings

    text = (ROOT / "docs" / "settings.md").read_text()

    def leaves(obj, prefix=""):
        for f in dataclasses.fields(obj):
            v = getattr(obj, f.name)
            name = f"{prefix}{f.name}"
            if dataclasses.is_dataclass(v):
                yield from leaves(v, f"{name}.")
            else:
                yield name

    missing = [n for n in leaves(TriggerSettings()) if n not in text]
    assert not missing, f"docs/settings.md does not mention: {missing}"


def test_the_readme_links_to_every_doc():
    readme = (ROOT / "README.md").read_text()
    for name in DOCS:
        assert f"docs/{name}" in readme, f"README does not link docs/{name}"


def test_the_docs_say_there_is_no_memory():
    """The most common wrong assumption about a thing like this."""
    for name in ("README.md", "docs/auto-reply.md"):
        assert "no memory" in (ROOT / name).read_text().lower(), name


def test_pypi_metadata_is_complete():
    """What someone sees before they read anything else.

    Absent, PyPI shows an unclassified package with no links — which reads as
    abandoned regardless of the state of the code.
    """
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    for key in ("classifiers", "keywords", "urls", "authors", "license",
                "license-files"):
        assert proj.get(key), f"pyproject [project] is missing {key}"
    assert "Repository" in proj["urls"]


def test_the_typed_claim_is_backed_by_a_marker():
    """Typing :: Typed without py.typed is a claim checkers cannot act on."""
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if "Typing :: Typed" in proj["classifiers"]:
        assert (ROOT / "wa_mcp" / "py.typed").is_file()


def test_no_license_classifier_alongside_a_license_expression():
    """PEP 639: setuptools refuses to build with both, and the failure is late."""
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if isinstance(proj.get("license"), str):
        bad = [c for c in proj["classifiers"] if c.startswith("License ::")]
        assert not bad, f"remove {bad} — superseded by the license expression"


def test_ci_installs_libmagic():
    """Without it every test errors on collection, blaming a Python package."""
    assert "libmagic" in (ROOT / ".github/workflows/ci.yml").read_text()


def test_security_policy_names_what_is_not_protected():
    """A policy listing only the defences reads as a claim of completeness.

    Prompt injection is mitigated here, not solved, and a token in a connector
    grants everything. Someone deploying this needs both stated.
    """
    text = (ROOT / "SECURITY.md").read_text()
    assert "What is not protected" in text
    assert "does not eliminate it" in text.lower() or "not eliminate" in text.lower()


# ------------------------------------------------------------- documentation

def _markdown_files():
    return sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")


@pytest.mark.parametrize("md", _markdown_files(), ids=lambda p: p.name)
def test_every_documentation_link_resolves(md):
    """A moved or merged doc leaves links behind that nothing catches.

    Folding models.md into auto-reply.md broke four links across three files —
    the sort of thing a reader hits on their first day and an author never
    does, because nobody re-reads their own README.
    """
    broken = []
    for text, link in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", md.read_text()):
        if link.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path, _, anchor = link.partition("#")
        target = (md.parent / path).resolve() if path else md
        if not target.exists():
            broken.append(f"[{text}]({link}) -> missing file")
            continue
        if anchor:
            heads = {_slug(h) for h in
                     re.findall(r"^#+ (.+)$", target.read_text(), re.M)}
            if anchor not in heads:
                broken.append(f"[{text}]({link}) -> no such heading")
    assert not broken, f"{md.name}: " + "; ".join(broken)


def test_the_readme_quick_start_matches_how_it_actually_runs():
    """The README's first code block is the only one most people run."""
    readme = (ROOT / "README.md").read_text()
    assert "python run.py" in readme
    assert (ROOT / "run.py").exists(), "README tells people to run a missing file"


def test_documented_environment_variables_exist():
    """A variable named in the README that config.py does not read is a
    setting someone will set, restart, and watch do nothing."""
    readme = (ROOT / "README.md").read_text()
    config = (ROOT / "wa_mcp" / "config.py").read_text()
    named = set(re.findall(r"`(WA_[A-Z_]+|PUBLIC_BASE_URL)`", readme))
    # WA_TEST_* select optional test backends; they belong to the suite,
    # not to the server's configuration.
    named = {v for v in named if not v.startswith("WA_TEST_")}
    missing = {v for v in named if v not in config}
    assert not missing, f"README documents unread variables: {sorted(missing)}"


def test_every_imported_package_is_declared():
    """The other direction, which is the one that breaks an install.

    A module that arrives transitively works on the machine it was written on
    and keeps working until the dependency that pulled it in changes. starlette
    reached us through fastmcp for the whole of this project's life while being
    imported by web.py.

    Every import counts, wherever it sits. The first version of this test
    skipped imports inside functions, reasoning that those are the optional
    backends — and starlette is imported inside a function, so the test passed
    with starlette undeclared, which is the exact bug it was written for. The
    optional backends are optional because they are declared in an extra, not
    because of where their import statement sits.
    """
    import ast
    import sys

    pyproject = (ROOT / "pyproject.toml").read_text()
    base = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    extras = pyproject.split("[project.optional-dependencies]", 1)[1] \
                      .split("[project.scripts]", 1)[0]
    declared = {d.split("[")[0].lower()
                for d in re.findall(r'"([A-Za-z0-9_.-]+)', base + extras)}
    # distribution name != import name
    declared |= {"dotenv", "pymongo"}          # python-dotenv; ships with motor

    imported = set()
    for path in (ROOT / "wa_mcp").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    undeclared = sorted(m for m in imported
                        if m not in sys.stdlib_module_names
                        and m != "wa_mcp"
                        and m.lower() not in declared)
    assert not undeclared, (
        f"imported but not declared in pyproject: {undeclared}")
