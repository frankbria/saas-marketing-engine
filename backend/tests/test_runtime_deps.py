"""S5.0.1 (#84): every module-scope third-party import in `app/` is a declared runtime dependency.

The bug this replaces: `httpx` was imported at module scope by the RunPod provisioner, the YouTube
uploader and both TTS paths, but declared only under `[dependency-groups] dev`. It resolved
because `anthropic` happened to pull it in. The GPU worker image installs `--no-dev` from the
lockfile, so the runtime/dev split is load-bearing there — an `anthropic` release that swapped its
HTTP client would have broken GPU provisioning and publishing at *import* time, in production,
with a traceback pointing at an unrelated package bump. Dependabot (#77) makes an unnoticed
transitive change more likely, not less.

Fixing the one dependency does not stop it recurring, so this asserts the whole class.

**Module scope specifically.** A deferred import inside a function is a different, milder risk:
it fails at call time and the caller can catch `ImportError` and say something useful (see
`_real_generate_music` and its `acestep` probe). A module-scope import fails the process on start.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"

# Third-party packages imported at module scope in `app/` that are deliberately NOT declared as
# direct runtime dependencies. Each entry needs a reason, and the reason has to be "this cannot
# disappear without its parent disappearing" — not "it works today".
ALLOWED_TRANSITIVE = {
    "sqlalchemy": "sqlmodel IS a SQLAlchemy wrapper and pins it tightly; it cannot vanish "
    "from under us, and an `==` pin here would fight sqlmodel's range on every bump",
    "redis": "pulled by the `celery[redis]` extra, whose entire purpose is to install it; "
    "the extra is the declaration",
}
# `prawcore` is deliberately absent: app/channels/reddit.py imports it inside a function, guarded
# by `except ImportError`, and degrades to "no auth classification available". That is the milder
# risk class this test does not police.


def _declared_runtime_distributions() -> set[str]:
    """Normalised distribution names from `[project] dependencies`, extras stripped."""
    data = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    names = set()
    for spec in data["project"]["dependencies"]:
        base = spec.split("==")[0].split(">=")[0].split("[")[0].strip()
        names.add(base.lower().replace("-", "_"))
    return names


def _module_scope_imports() -> dict[str, set[str]]:
    """Top-level third-party module name → the `app/` files importing it at module scope."""
    imports: dict[str, set[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:  # module scope only — not walk()
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `from . import x` / `from .foo import x` have no module name to resolve
                modules = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top in sys.stdlib_module_names or top == "app":
                    continue
                imports.setdefault(top, set()).add(str(path.relative_to(BACKEND)))
    return imports


def test_every_module_scope_import_is_a_declared_runtime_dependency():
    declared = _declared_runtime_distributions()
    top_level_to_dists = packages_distributions()

    undeclared: dict[str, set[str]] = {}
    for top, files in _module_scope_imports().items():
        if top in ALLOWED_TRANSITIVE:
            continue
        provided_by = {d.lower().replace("-", "_") for d in top_level_to_dists.get(top, [])}
        if not provided_by & declared:
            undeclared[top] = files

    assert not undeclared, (
        "module-scope imports in app/ with no declared runtime dependency:\n"
        + "\n".join(f"  {top}: {', '.join(sorted(files))}" for top, files in undeclared.items())
        + "\n\nAdd an `==`-pinned entry to [project] dependencies in pyproject.toml, or — if the "
        "package genuinely cannot exist apart from a declared parent — add it to "
        "ALLOWED_TRANSITIVE in this file with the reason."
    )


def test_httpx_is_a_runtime_dependency_not_a_dev_one():
    """The specific regression: httpx back in dev-only would silently re-break the GPU image."""
    data = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    runtime = [d for d in data["project"]["dependencies"] if d.lower().startswith("httpx")]
    dev = [d for d in data["dependency-groups"]["dev"] if d.lower().startswith("httpx")]

    assert runtime, "httpx must be in [project] dependencies — the GPU image installs --no-dev"
    assert runtime[0].startswith("httpx=="), f"house style is an == pin, got {runtime[0]!r}"
    assert not dev, "httpx in both groups would be ambiguous; the runtime declaration covers tests"


def test_allowed_transitive_entries_are_still_imported():
    """Stop the allowlist rotting into a list of exceptions for imports nobody makes any more."""
    imported = set(_module_scope_imports())
    stale = set(ALLOWED_TRANSITIVE) - imported
    assert not stale, f"ALLOWED_TRANSITIVE lists packages app/ no longer imports: {sorted(stale)}"
