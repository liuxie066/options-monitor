from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from scripts.multi_tick.misc import atomic_symlink, update_legacy_output_link


def test_atomic_symlink_uses_runtime_tmp_dir_not_repo_root() -> None:
    with TemporaryDirectory() as td:
        repo_root = Path(td) / 'repo'
        repo_root.mkdir()
        target = repo_root / 'output_accounts' / 'lx'
        target.mkdir(parents=True)
        path = repo_root / 'output'
        runtime_tmp = repo_root / 'output_shared' / 'tmp' / 'legacy_output_link'

        real_replace = os.replace
        seen: dict[str, Path] = {}

        def fake_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
            seen['src'] = Path(src)
            seen['dst'] = Path(dst)
            real_replace(src, dst)

        os.replace = fake_replace  # type: ignore[assignment]
        try:
            atomic_symlink(path, target, tmp_dir=runtime_tmp)
        finally:
            os.replace = real_replace  # type: ignore[assignment]

        assert seen['src'] == runtime_tmp / 'output.tmp'
        assert seen['dst'] == path
        assert not (repo_root / 'output.tmp').exists()
        assert path.is_symlink()
        assert path.resolve() == target.resolve()


def test_update_legacy_output_link_skips_readonly_root_without_output_tmp() -> None:
    with TemporaryDirectory() as td:
        repo_root = Path(td) / 'repo'
        repo_root.mkdir()
        old_target = repo_root / 'output_accounts' / 'old'
        new_target = repo_root / 'output_accounts' / 'lx'
        old_target.mkdir(parents=True)
        new_target.mkdir(parents=True)
        out_link = repo_root / 'output'
        out_link.symlink_to(old_target, target_is_directory=True)
        runtime_tmp = repo_root / 'output_shared' / 'tmp' / 'legacy_output_link'
        runtime_tmp.mkdir(parents=True)

        real_access = os.access

        def fake_access(path: str | os.PathLike[str], mode: int, *args) -> bool:
            if Path(path) == repo_root and mode == os.W_OK:
                return False
            return real_access(path, mode, *args)

        os.access = fake_access  # type: ignore[assignment]
        try:
            updated = update_legacy_output_link(out_link, new_target, tmp_dir=runtime_tmp)
        finally:
            os.access = real_access  # type: ignore[assignment]

        assert updated is False
        assert not (repo_root / 'output.tmp').exists()
        assert out_link.is_symlink()
        assert out_link.resolve() == old_target.resolve()


def main() -> None:
    test_atomic_symlink_uses_runtime_tmp_dir_not_repo_root()
    test_update_legacy_output_link_skips_readonly_root_without_output_tmp()
    print('OK (multi-tick-output-link-runtime-tmp)')


if __name__ == '__main__':
    main()
