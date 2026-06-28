"""Per-product workspace on disk.

Each product gets an isolated directory holding its generated assets (site, content,
metrics) and an empty credentials vault. S0.3 creates the empty vault directory; S0.4
adds the Fernet-encrypted credential store inside it. Paths derive from the product
slug — never from a hardcoded product name (PRD G7).
"""

import shutil
from pathlib import Path

from app.config import settings


def workspace_path(slug: str) -> Path:
    return Path(settings.workspace_root) / slug


def create_workspace(slug: str) -> Path:
    """Create `{workspace_root}/{slug}/` + an empty `vault/`. Idempotent."""
    root = workspace_path(slug)
    (root / "vault").mkdir(parents=True, exist_ok=True)
    return root


def remove_workspace(slug: str) -> None:
    """Delete a product's workspace tree. No-op if it doesn't exist."""
    shutil.rmtree(workspace_path(slug), ignore_errors=True)
