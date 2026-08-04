from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest


def _account_config(*, account: str = "lx", marker: str = "a") -> dict:
    return {
        "portfolio": {"account": account},
        "runtime": {"marker": marker},
        "symbols": [],
    }


def test_prepare_tick_run_workspace_creates_required_dirs(tmp_path) -> None:
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
    assert workspace.run_dir.is_dir()
    assert (workspace.shared_required / "raw").is_dir()
    assert (workspace.shared_required / "parsed").is_dir()
    assert (tmp_path / "output_runs" / "20260513T010203" / "state").is_dir()
    pointer = tmp_path / "output_shared" / "state" / "last_run_dir.txt"
    assert pointer.read_text(encoding="utf-8").strip() == str(workspace.run_dir)


def test_prepare_tick_run_workspace_preserves_historical_runs(tmp_path) -> None:
    import os
    import time

    from src.application.tick_run_workspace import prepare_tick_run_workspace

    historical_run = tmp_path / "output_runs" / "20260101T000000"
    historical_run.mkdir(parents=True)
    old_timestamp = time.time() - 8 * 86400
    os.utime(historical_run, (old_timestamp, old_timestamp))

    prepare_tick_run_workspace(
        base=tmp_path,
        run_id="20260719T120000",
        default_account="lx",
    )

    assert historical_run.is_dir()


def test_account_config_publication_is_run_scoped_under_overlap(tmp_path) -> None:
    from src.application.tick_run_workspace import (
        load_account_run_config,
        publish_account_run_config,
    )

    historical = tmp_path / "output_accounts" / "lx" / "state" / "config.override.json"
    historical.parent.mkdir(parents=True)
    historical.write_text("historical\n", encoding="utf-8")

    def _publish(run_id: str, marker: str):
        return publish_account_run_config(
            base=tmp_path,
            run_id=run_id,
            account="lx",
            config=_account_config(marker=marker),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_publish, "run-a", "a")
        second = executor.submit(_publish, "run-b", "b")
        authority_a = first.result()
        authority_b = second.result()

    assert authority_a.state_path != authority_b.state_path
    assert authority_a.compatibility_path != authority_b.compatibility_path
    assert authority_a.account_config_sha256 != authority_b.account_config_sha256
    assert "/run-a/accounts/lx/" in authority_a.state_path.as_posix()
    assert "/run-b/accounts/lx/" in authority_b.state_path.as_posix()
    assert (
        load_account_run_config(
            authority=authority_a,
            base=tmp_path,
            run_id="run-a",
            account="lx",
        )["runtime"]["marker"]
        == "a"
    )
    assert (
        load_account_run_config(
            authority=authority_b,
            base=tmp_path,
            run_id="run-b",
            account="lx",
        )["runtime"]["marker"]
        == "b"
    )
    assert historical.read_text(encoding="utf-8") == "historical\n"


def test_same_run_identical_config_is_adopted_concurrently(tmp_path) -> None:
    from src.application.tick_run_workspace import publish_account_run_config

    config = _account_config(marker="same")
    with ThreadPoolExecutor(max_workers=8) as executor:
        authorities = list(
            executor.map(
                lambda _index: publish_account_run_config(
                    base=tmp_path,
                    run_id="run-same",
                    account="lx",
                    config=config,
                ),
                range(16),
            )
        )

    assert len({item.state_path for item in authorities}) == 1
    assert len({item.compatibility_path for item in authorities}) == 1
    assert len({item.account_config_sha256 for item in authorities}) == 1
    authority = authorities[0]
    assert authority.state_path.read_bytes() == authority.canonical_bytes
    assert authority.compatibility_path.read_bytes() == authority.canonical_bytes
    assert not list(authority.state_path.parent.glob(".*.tmp"))


def test_same_run_different_config_fails_without_overwrite(tmp_path) -> None:
    from src.application.tick_run_workspace import (
        AccountRunConfigError,
        publish_account_run_config,
    )

    original = publish_account_run_config(
        base=tmp_path,
        run_id="run-conflict",
        account="lx",
        config=_account_config(marker="original"),
    )
    state_before = original.state_path.read_bytes()
    compatibility_before = original.compatibility_path.read_bytes()

    with pytest.raises(AccountRunConfigError) as raised:
        publish_account_run_config(
            base=tmp_path,
            run_id="run-conflict",
            account="lx",
            config=_account_config(marker="replacement"),
        )

    assert raised.value.code == "ACCOUNT_CONFIG_STATE_CONFLICT"
    assert original.state_path.read_bytes() == state_before
    assert original.compatibility_path.read_bytes() == compatibility_before


def test_account_config_authority_rejects_cross_run_consumption(tmp_path) -> None:
    from src.application.tick_run_workspace import (
        AccountRunConfigError,
        load_account_run_config,
        publish_account_run_config,
    )

    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-a",
        account="lx",
        config=_account_config(),
    )

    with pytest.raises(AccountRunConfigError) as raised:
        load_account_run_config(
            authority=replace(authority, run_id="run-b"),
            base=tmp_path,
            run_id="run-b",
            account="lx",
        )

    assert raised.value.code == "ACCOUNT_CONFIG_PATH_MISMATCH"


@pytest.mark.parametrize(
    "account",
    ["../lx", "/tmp/lx", "lx/sy", r"lx\sy", ".", "lx.sy"],
)
def test_account_config_publication_rejects_unsafe_account_before_writes(
    tmp_path,
    account: str,
) -> None:
    from src.application.tick_run_workspace import (
        AccountRunConfigError,
        publish_account_run_config,
    )

    with pytest.raises(AccountRunConfigError) as raised:
        publish_account_run_config(
            base=tmp_path,
            run_id="run-safe",
            account=account,
            config=_account_config(account=account),
        )

    assert raised.value.code == "ACCOUNT_CONFIG_IDENTITY_INVALID"
    assert not (tmp_path / "output_runs").exists()


def test_account_config_semantic_scope_is_validated_before_writes(tmp_path) -> None:
    from src.application.tick_run_workspace import (
        AccountRunConfigError,
        publish_account_run_config,
    )

    with pytest.raises(AccountRunConfigError) as raised:
        publish_account_run_config(
            base=tmp_path,
            run_id="run-semantic-mismatch",
            account="lx",
            config=_account_config(account="sy"),
        )

    assert raised.value.code == "ACCOUNT_CONFIG_ACCOUNT_MISMATCH"
    assert not (tmp_path / "output_runs").exists()


def test_account_config_publication_rejects_symlinked_ancestor(tmp_path) -> None:
    from src.application.tick_run_workspace import (
        AccountRunConfigError,
        publish_account_run_config,
    )

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "output_runs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AccountRunConfigError) as raised:
        publish_account_run_config(
            base=tmp_path,
            run_id="run-symlink",
            account="lx",
            config=_account_config(),
        )

    assert raised.value.code == "ACCOUNT_CONFIG_STATE_WRITE_FAILED"
    assert list(outside.iterdir()) == []


def test_account_config_load_rejects_account_directory_symlink_swap(tmp_path) -> None:
    from src.application.tick_run_workspace import (
        AccountRunConfigError,
        load_account_run_config,
        publish_account_run_config,
    )

    authority = publish_account_run_config(
        base=tmp_path,
        run_id="run-swap",
        account="lx",
        config=_account_config(),
    )
    account_dir = authority.compatibility_path.parent
    preserved = account_dir.with_name("lx-preserved")
    account_dir.rename(preserved)
    outside = tmp_path / "outside-account"
    outside.mkdir()
    account_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(AccountRunConfigError) as raised:
        load_account_run_config(
            authority=authority,
            base=tmp_path,
            run_id="run-swap",
            account="lx",
        )

    assert raised.value.code == "ACCOUNT_CONFIG_STATE_UNAVAILABLE"
    assert list(outside.iterdir()) == []


def test_prepare_workspace_rejects_unsafe_default_account_before_writes(
    tmp_path,
) -> None:
    from src.application.tick_run_workspace import (
        AccountRunConfigError,
        prepare_tick_run_workspace,
    )

    with pytest.raises(AccountRunConfigError) as raised:
        prepare_tick_run_workspace(
            base=tmp_path,
            run_id="run-safe",
            default_account="../lx",
        )

    assert raised.value.code == "ACCOUNT_CONFIG_IDENTITY_INVALID"
    assert list(tmp_path.iterdir()) == []
