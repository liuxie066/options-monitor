from __future__ import annotations


def test_prepare_tick_run_workspace_creates_required_dirs_and_legacy_link(tmp_path) -> None:
    from src.application.tick_run_workspace import prepare_tick_run_workspace

    workspace = prepare_tick_run_workspace(
        base=tmp_path,
        run_id="20260513T010203",
        default_account="lx",
    )

    assert workspace.accounts_root == (tmp_path / "output_accounts").resolve()
    assert (workspace.accounts_root / "lx" / "raw").is_dir()
    assert (workspace.accounts_root / "lx" / "parsed").is_dir()
    assert (workspace.accounts_root / "lx" / "reports").is_dir()
    assert (workspace.accounts_root / "lx" / "state").is_dir()
    assert workspace.out_link.is_symlink()
    assert workspace.out_link.resolve() == (workspace.accounts_root / "lx").resolve()
    assert workspace.run_dir.is_dir()
    assert (workspace.shared_required / "raw").is_dir()
    assert (workspace.shared_required / "parsed").is_dir()
    assert (tmp_path / "output_runs" / "20260513T010203" / "state").is_dir()
    pointer = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
    assert pointer.read_text(encoding="utf-8").strip() == str(workspace.run_dir)
