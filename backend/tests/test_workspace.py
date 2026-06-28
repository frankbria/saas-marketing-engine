"""S0.3: per-product workspace + empty credentials vault on disk."""

from app import workspace


def test_create_workspace_makes_dirs_and_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace.settings, "workspace_root", str(tmp_path))
    root = workspace.create_workspace("auto-author")

    assert root.is_dir()
    assert (root / "vault").is_dir()
    assert root == tmp_path / "auto-author"


def test_create_workspace_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace.settings, "workspace_root", str(tmp_path))
    workspace.create_workspace("widget")
    workspace.create_workspace("widget")  # no error on second call
    assert (tmp_path / "widget" / "vault").is_dir()


def test_remove_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace.settings, "workspace_root", str(tmp_path))
    workspace.create_workspace("gone")
    workspace.remove_workspace("gone")
    assert not (tmp_path / "gone").exists()
    workspace.remove_workspace("gone")  # no error when already absent
