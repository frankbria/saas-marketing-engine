"""Repo ingest for the strategy module (TECH_SPEC §5 step 1).

Collect a *bounded* set of high-signal files (README, manifests, docs, route/UI source) — never
the whole repo. The per-file summarize→synthesize flow in brief.py caps token use; this module
caps which files even reach that flow. Local clone preferred; clone `repo_url` if no local path.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Caps keep token use bounded regardless of repo size (§5 "no whole-repo dump").
MAX_FILES = 40
MAX_BYTES = 8_000  # per file; truncated past this

_MANIFESTS = {
    "pyproject.toml",
    "package.json",
    "requirements.txt",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "gemfile",
    "composer.json",
    "setup.py",
    "pipfile",
}
# Dirs never worth reading for marketing signal.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", ".next"}
# Source extensions we sample for route/endpoint names + UI copy.
_SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx"}
# Path hints that flag a source file as carrying marketing signal: routes/endpoints AND the
# user-facing copy that lives in UI components/pages/sections (TECH_SPEC §5 "UI copy").
_SIGNAL_HINTS = (
    "route",
    "api",
    "page",
    "view",
    "endpoint",
    "url",
    "component",
    "ui",
    "copy",
    "content",
    "landing",
    "hero",
    "pricing",
    "section",
)


def resolve_repo(repo_local_path: str | None, repo_url: str | None, dest: Path) -> Path:
    """Return a local path to the product's repo, cloning `repo_url` shallowly if needed."""
    if repo_local_path:
        path = Path(repo_local_path)
        if not path.is_dir():
            raise FileNotFoundError(f"repo_local_path does not exist: {repo_local_path}")
        return path
    if repo_url:
        # Always re-clone fresh: a cached checkout goes stale when the upstream repo changes or
        # the product's repo_url is edited, and the brief must reflect the current repo.
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        return dest
    raise ValueError("product has neither repo_local_path nor repo_url")


def _priority(relpath: str) -> int:
    """Lower sorts first. README and manifests are the highest-signal files."""
    name = Path(relpath).name.lower()
    if name.startswith("readme"):
        return 0
    if name in _MANIFESTS:
        return 1
    if relpath.lower().endswith(".md"):
        return 2
    if any(h in relpath.lower() for h in _SIGNAL_HINTS):
        return 3
    return 4


def _candidate(repo: Path, path: Path) -> bool:
    name = path.name.lower()
    if name.startswith("readme") or name in _MANIFESTS or path.suffix.lower() == ".md":
        return True
    if path.suffix.lower() in _SOURCE_EXTS and any(
        h in str(path.relative_to(repo)).lower() for h in _SIGNAL_HINTS
    ):
        return True
    return False


def _within(repo_root: Path, path: Path) -> bool:
    """True only if `path` resolves to a location inside the repo — blocks symlink escapes.

    Untrusted repos can contain a symlink named e.g. README.md pointing at /etc/passwd; without
    this check `read_text` would follow it and ship host files to the LLM. Catches symlinked files
    and files reached through symlinked directories alike.
    """
    try:
        path.resolve().relative_to(repo_root)
        return True
    except (OSError, ValueError):
        return False


def collect_signal_files(repo: Path) -> list[tuple[str, str]]:
    """Return [(relpath, text)] for up to MAX_FILES high-signal files, each capped at MAX_BYTES."""
    repo_root = repo.resolve()
    found: list[Path] = []
    for path in repo.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if not _within(repo_root, path):
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(repo).parts):
            continue
        if _candidate(repo, path):
            found.append(path)

    found.sort(key=lambda p: (_priority(str(p.relative_to(repo))), str(p.relative_to(repo))))

    out: list[tuple[str, str]] = []
    for path in found[:MAX_FILES]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:MAX_BYTES]
        except OSError:
            continue
        if text.strip():
            out.append((str(path.relative_to(repo)), text))
    return out
