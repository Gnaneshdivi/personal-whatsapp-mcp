from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

OS_VARS = {"LOCALAPPDATA", "XDG_DATA_HOME"}
ALIASES = {"MCP_AUTH_TOKEN"}


def _env_read_by_config() -> set[str]:
    src = (ROOT / "wa_mcp" / "config.py").read_text()
    return set(re.findall(r'(?:getenv|_flag)\(\s*"([A-Z_]+)"', src)) - OS_VARS - ALIASES


def _env_documented() -> set[str]:
    return set(re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))


def test_every_setting_is_documented():
    missing = sorted(_env_read_by_config() - _env_documented())
    assert not missing, f".env.example does not mention: {missing}"


def test_nothing_documented_has_been_removed():
    stale = sorted(_env_documented() - _env_read_by_config())
    assert not stale, f".env.example documents settings nothing reads: {stale}"


def test_declared_dependencies_are_actually_imported():
    pyproject = (ROOT / "pyproject.toml").read_text()
    block = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    declared = re.findall(r'"([A-Za-z0-9_.-]+)', block)

    src = "\n".join(p.read_text() for p in (ROOT / "wa_mcp").rglob("*.py"))
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
    pyproject = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'readme\s*=\s*"([^"]+)"', pyproject)
    assert m and (ROOT / m.group(1)).is_file()


def test_the_dockerfile_installs_libmagic():
    assert "libmagic" in (ROOT / "Dockerfile").read_text()


def test_the_dockerfile_persists_the_session():
    df = (ROOT / "Dockerfile").read_text()
    assert "VOLUME" in df and "WA_DATA_DIR" in df


DOCS = ["setup.md", "settings.md", "auto-reply.md",
        "recipes.md", "architecture.md"]


@pytest.mark.parametrize("name", DOCS)
def test_the_docs_exist(name):
    p = ROOT / "docs" / name
    assert p.is_file() and p.stat().st_size > 500


def test_every_auto_reply_setting_is_documented():
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
    for name in ("README.md", "docs/auto-reply.md"):
        assert "no memory" in (ROOT / name).read_text().lower(), name


def test_pypi_metadata_is_complete():
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    for key in ("classifiers", "keywords", "urls", "authors", "license",
                "license-files"):
        assert proj.get(key), f"pyproject [project] is missing {key}"
    assert "Repository" in proj["urls"]


def test_the_typed_claim_is_backed_by_a_marker():
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if "Typing :: Typed" in proj["classifiers"]:
        assert (ROOT / "wa_mcp" / "py.typed").is_file()


def test_no_license_classifier_alongside_a_license_expression():
    import tomllib

    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if isinstance(proj.get("license"), str):
        bad = [c for c in proj["classifiers"] if c.startswith("License ::")]
        assert not bad, f"remove {bad} — superseded by the license expression"


def test_ci_installs_libmagic():
    assert "libmagic" in (ROOT / ".github/workflows/ci.yml").read_text()


def test_security_policy_names_what_is_not_protected():
    text = (ROOT / "SECURITY.md").read_text()
    assert "What is not protected" in text
    assert "does not eliminate it" in text.lower() or "not eliminate" in text.lower()


def _markdown_files():
    return sorted(ROOT.glob("*.md")) + sorted((ROOT / "docs").glob("*.md"))


def _slug(heading: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", heading.strip().lower()).strip("-")


@pytest.mark.parametrize("md", _markdown_files(), ids=lambda p: p.name)
def test_every_documentation_link_resolves(md):
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
    readme = (ROOT / "README.md").read_text()
    assert "python run.py" in readme
    assert (ROOT / "run.py").exists(), "README tells people to run a missing file"


def test_documented_environment_variables_exist():
    readme = (ROOT / "README.md").read_text()
    config = (ROOT / "wa_mcp" / "config.py").read_text()
    named = set(re.findall(r"`(WA_[A-Z_]+|PUBLIC_BASE_URL)`", readme))
    named = {v for v in named if not v.startswith("WA_TEST_")}
    missing = {v for v in named if v not in config}
    assert not missing, f"README documents unread variables: {sorted(missing)}"


def test_every_imported_package_is_declared():
    import ast
    import sys

    pyproject = (ROOT / "pyproject.toml").read_text()
    base = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    extras = pyproject.split("[project.optional-dependencies]", 1)[1] \
                      .split("[project.scripts]", 1)[0]
    declared = {d.split("[")[0].lower()
                for d in re.findall(r'"([A-Za-z0-9_.-]+)', base + extras)}
    declared |= {"dotenv", "pymongo"}

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


def test_referenced_images_exist():
    """A screenshot named in the docs but missing from assets/ renders as a
    broken image on GitHub, which looks worse than having no screenshot.

    Resolved against the file doing the referencing, not the repo root: the
    README says assets/x.png and docs/setup.md says ../assets/x.png, and both
    are correct from where they sit.
    """
    problems = []
    for md in _markdown_files():
        for alt, link in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", md.read_text()):
            if link.startswith(("http://", "https://")):
                continue
            if not (md.parent / link.split("#")[0]).exists():
                problems.append(f"{md.name}: {link} is missing")
            elif not alt.strip():
                problems.append(f"{md.name}: {link} has no alt text")
    assert not problems, problems


def test_no_screenshot_leaks_the_access_token():
    """The MCP endpoint shown in Settings carries the token, and the token is
    the whole credential. A screenshot of that page published to a public repo
    hands over the account, and deleting the file later does not help because
    git keeps it."""
    docs = " ".join(md.read_text() for md in _markdown_files())
    leaked = re.findall(r"[?&]k=([A-Za-z0-9_-]{16,})", docs)
    assert not leaked, f"a real-looking token appears in the docs: {leaked}"
