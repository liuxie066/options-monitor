from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from typing import Any
from uuid import uuid4

from domain.storage.repositories import run_repo, state_repo
from src.application.account_config import normalize_account_label
from src.application.multi_tick.misc import (
    ensure_account_output_dir,
)


ACCOUNT_RUN_CONFIG_NAME = "config.override.json"


class AccountRunConfigError(RuntimeError):
    """Typed failure while publishing or consuming one run's account config."""

    def __init__(self, code: str, message: str) -> None:
        normalized = str(code or "ACCOUNT_CONFIG_AUTHORITY_INVALID").strip().upper()
        self.code = normalized
        super().__init__(str(message))

    @property
    def reason(self) -> str:
        return self.code.lower()


@dataclass(frozen=True)
class AccountRunConfigAuthority:
    run_id: str
    account: str
    state_path: Path
    compatibility_path: Path
    account_config_sha256: str
    canonical_bytes: bytes = field(repr=False)


@dataclass(frozen=True)
class TickRunWorkspace:
    accounts_root: Path
    run_dir: Path
    shared_required: Path


def canonical_account_run_config_bytes(config: Mapping[str, Any]) -> bytes:
    """Serialize one account config once into its canonical immutable bytes."""

    try:
        payload = json.dumps(
            dict(config),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_SERIALIZATION_FAILED",
            "account runtime config is not canonical JSON",
        ) from exc
    return (payload + "\n").encode("utf-8")


def account_run_config_paths(
    *,
    base: Path,
    run_id: str,
    account: str,
) -> tuple[Path, Path]:
    """Return the canonical state path and run-account compatibility path."""

    run_id_norm = _identity_component(run_id, "run_id")
    account_norm = _account_identity_component(account)
    run_account_dir = Path(base).resolve() / "output_runs" / run_id_norm / "accounts" / account_norm
    return (
        run_account_dir / "state" / ACCOUNT_RUN_CONFIG_NAME,
        run_account_dir / ACCOUNT_RUN_CONFIG_NAME,
    )


def publish_account_run_config(
    *,
    base: Path,
    run_id: str,
    account: str,
    config: Mapping[str, Any],
) -> AccountRunConfigAuthority:
    """Atomically publish or adopt both immutable copies for one account run."""

    run_id_norm = _identity_component(run_id, "run_id")
    account_norm = _account_identity_component(account)
    payload = canonical_account_run_config_bytes(config)
    _decode_account_run_config(payload, expected_account=account_norm)
    digest = sha256(payload).hexdigest()
    state_path, compatibility_path = account_run_config_paths(
        base=base,
        run_id=run_id_norm,
        account=account_norm,
    )
    _write_once_or_adopt_at(
        base=base,
        components=("output_runs", run_id_norm, "accounts", account_norm, "state"),
        name=ACCOUNT_RUN_CONFIG_NAME,
        payload=payload,
        conflict_code="ACCOUNT_CONFIG_STATE_CONFLICT",
        write_code="ACCOUNT_CONFIG_STATE_WRITE_FAILED",
    )
    _write_once_or_adopt_at(
        base=base,
        components=("output_runs", run_id_norm, "accounts", account_norm),
        name=ACCOUNT_RUN_CONFIG_NAME,
        payload=payload,
        conflict_code="ACCOUNT_CONFIG_COMPATIBILITY_CONFLICT",
        write_code="ACCOUNT_CONFIG_COMPATIBILITY_WRITE_FAILED",
    )
    authority = AccountRunConfigAuthority(
        run_id=run_id_norm,
        account=account_norm,
        state_path=state_path,
        compatibility_path=compatibility_path,
        account_config_sha256=digest,
        canonical_bytes=payload,
    )
    load_account_run_config(
        authority=authority,
        base=base,
        run_id=run_id_norm,
        account=account_norm,
    )
    return authority


def load_account_run_config(
    *,
    authority: AccountRunConfigAuthority,
    base: Path,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    """Validate a parent-retained authority and load the exact published bytes."""

    retained = load_retained_account_run_config(
        authority=authority,
        base=base,
        run_id=run_id,
        account=account,
    )
    load_published_account_run_config(
        base=base,
        run_id=authority.run_id,
        account=authority.account,
        state_path=authority.state_path,
        compatibility_path=authority.compatibility_path,
        account_config_sha256=authority.account_config_sha256,
        expected_bytes=authority.canonical_bytes,
    )
    return retained


def load_retained_account_run_config(
    *,
    authority: AccountRunConfigAuthority,
    base: Path,
    run_id: str,
    account: str,
) -> dict[str, Any]:
    """Decode the immutable generation retained by the parent process.

    Publication validates both on-disk artifacts once.  After the final
    pre-side-effect barrier, consumers use these retained bytes so a later
    path replacement cannot split parent and child configuration semantics.
    """

    if not isinstance(authority, AccountRunConfigAuthority):
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_AUTHORITY_INVALID",
            "account config authority is missing or invalid",
        )
    run_id_norm = _identity_component(run_id, "run_id")
    account_norm = _account_identity_component(account)
    if authority.run_id != run_id_norm or authority.account != account_norm:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_IDENTITY_MISMATCH",
            "account config authority does not match the requested run/account",
        )
    expected_state, expected_compatibility = account_run_config_paths(
        base=base,
        run_id=run_id_norm,
        account=account_norm,
    )
    if (
        _absolute_without_symlink_resolution(authority.state_path) != expected_state
        or _absolute_without_symlink_resolution(authority.compatibility_path)
        != expected_compatibility
    ):
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PATH_MISMATCH",
            "account config authority paths are outside the requested run/account",
        )
    if sha256(authority.canonical_bytes).hexdigest() != authority.account_config_sha256:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PARENT_BYTES_MISMATCH",
            "parent-retained account config bytes do not match their hash",
        )
    return _decode_account_run_config(
        authority.canonical_bytes,
        expected_account=account_norm,
    )


def write_account_run_state_json_safely(
    *,
    base: Path,
    run_id: str,
    account: str,
    name: str,
    payload: Mapping[str, Any],
) -> Path:
    """Atomically replace one run/account state file through a no-follow chain."""

    run_id_norm = _identity_component(run_id, "run_id")
    account_norm = _account_identity_component(account)
    name_norm = _identity_component(name, "state file name")
    encoded = (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    _atomic_replace_at(
        base=base,
        components=(
            "output_runs",
            run_id_norm,
            "accounts",
            account_norm,
            "state",
        ),
        name=name_norm,
        payload=encoded,
        code="ACCOUNT_RUN_STATE_WRITE_FAILED",
    )
    return (
        Path(base).resolve()
        / "output_runs"
        / run_id_norm
        / "accounts"
        / account_norm
        / "state"
        / name_norm
    )


def write_account_run_state_bytes_once_safely(
    *,
    base: Path,
    run_id: str,
    account: str,
    name: str,
    payload: bytes,
) -> Path:
    """Publish or adopt immutable run/account state bytes via a no-follow chain."""

    run_id_norm = _identity_component(run_id, "run_id")
    account_norm = _account_identity_component(account)
    name_norm = _identity_component(name, "state file name")
    if not isinstance(payload, bytes):
        raise AccountRunConfigError(
            "ACCOUNT_RUN_STATE_PAYLOAD_INVALID",
            "run-account state payload must be bytes",
        )
    _write_once_or_adopt_at(
        base=base,
        components=(
            "output_runs",
            run_id_norm,
            "accounts",
            account_norm,
            "state",
        ),
        name=name_norm,
        payload=payload,
        conflict_code="ACCOUNT_RUN_STATE_CONFLICT",
        write_code="ACCOUNT_RUN_STATE_WRITE_FAILED",
    )
    return (
        Path(base).resolve()
        / "output_runs"
        / run_id_norm
        / "accounts"
        / account_norm
        / "state"
        / name_norm
    )


def read_account_run_state_bytes_safely(
    *,
    base: Path,
    run_id: str,
    account: str,
    name: str,
) -> bytes:
    """Read immutable run/account state bytes through a no-follow chain."""

    run_id_norm = _identity_component(run_id, "run_id")
    account_norm = _account_identity_component(account)
    name_norm = _identity_component(name, "state file name")
    return _read_regular_file_at_chain(
        base=base,
        components=(
            "output_runs",
            run_id_norm,
            "accounts",
            account_norm,
            "state",
        ),
        name=name_norm,
        code="ACCOUNT_RUN_STATE_UNAVAILABLE",
    )


def ensure_run_state_directory_safely(*, base: Path, run_id: str) -> Path:
    """Create the shared run state directory through a no-follow chain."""

    run_id_norm = _identity_component(run_id, "run_id")
    descriptor = _open_directory_chain(
        base=base,
        components=("output_runs", run_id_norm, "state"),
        create=True,
        code="ACCOUNT_RUN_STATE_WRITE_FAILED",
    )
    os.close(descriptor)
    return Path(base).resolve() / "output_runs" / run_id_norm / "state"


def load_published_account_run_config(
    *,
    base: Path,
    run_id: str,
    account: str,
    state_path: Path,
    compatibility_path: Path,
    account_config_sha256: str,
    expected_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Validate both published artifacts and decode the exact state bytes."""

    run_id_norm = _identity_component(run_id, "run_id")
    account_norm = _account_identity_component(account)
    expected_state, expected_compatibility = account_run_config_paths(
        base=base,
        run_id=run_id_norm,
        account=account_norm,
    )
    actual_state = _absolute_without_symlink_resolution(state_path)
    actual_compatibility = _absolute_without_symlink_resolution(compatibility_path)
    if actual_state != expected_state or actual_compatibility != expected_compatibility:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PATH_MISMATCH",
            "account config authority paths are outside the requested run/account",
        )
    digest = str(account_config_sha256 or "").strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_HASH_INVALID",
            "account config authority hash is invalid",
        )
    state_bytes = _read_regular_file_at_chain(
        base=base,
        components=("output_runs", run_id_norm, "accounts", account_norm, "state"),
        name=ACCOUNT_RUN_CONFIG_NAME,
        code="ACCOUNT_CONFIG_STATE_UNAVAILABLE",
    )
    compatibility_bytes = _read_regular_file_at_chain(
        base=base,
        components=("output_runs", run_id_norm, "accounts", account_norm),
        name=ACCOUNT_RUN_CONFIG_NAME,
        code="ACCOUNT_CONFIG_COMPATIBILITY_UNAVAILABLE",
    )
    if state_bytes != compatibility_bytes:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_ARTIFACT_MISMATCH",
            "state and compatibility account config bytes differ",
        )
    if expected_bytes is not None and state_bytes != expected_bytes:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PARENT_BYTES_MISMATCH",
            "published account config differs from parent-retained bytes",
        )
    if sha256(state_bytes).hexdigest() != digest:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_HASH_MISMATCH",
            "published account config does not match its authority hash",
        )
    return _decode_account_run_config(state_bytes, expected_account=account_norm)


def _write_once_or_adopt_at(
    *,
    base: Path,
    components: tuple[str, ...],
    name: str,
    payload: bytes,
    conflict_code: str,
    write_code: str,
) -> None:
    parent_descriptor = _open_directory_chain(
        base=base,
        components=components,
        create=True,
        code=write_code,
    )
    temp_name = f".{name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                temp_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o644,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise AccountRunConfigError(
                write_code,
                f"cannot stage account config artifact {name}",
            ) from exc

        try:
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("account config staging write made no progress")
                written += count
            os.fsync(descriptor)
        except OSError as exc:
            raise AccountRunConfigError(
                write_code,
                f"cannot write account config artifact {name}",
            ) from exc
        finally:
            os.close(descriptor)
            descriptor = None

        try:
            os.link(
                temp_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_regular_file_at(
                parent_descriptor,
                name,
                code=conflict_code,
            )
            if existing != payload:
                raise AccountRunConfigError(
                    conflict_code,
                    f"existing account config artifact conflicts at {name}",
                )
        except OSError as exc:
            raise AccountRunConfigError(
                write_code,
                f"cannot publish account config artifact {name}",
            ) from exc
        try:
            os.fsync(parent_descriptor)
        except OSError:
            pass
        published = _read_regular_file_at(
            parent_descriptor,
            name,
            code=write_code,
        )
        if published != payload:
            raise AccountRunConfigError(
                conflict_code,
                f"published account config artifact changed at {name}",
            )
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(parent_descriptor)


def _atomic_replace_at(
    *,
    base: Path,
    components: tuple[str, ...],
    name: str,
    payload: bytes,
    code: str,
) -> None:
    parent_descriptor = _open_directory_chain(
        base=base,
        components=components,
        create=True,
        code=code,
    )
    temp_name = f".{name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o644,
            dir_fd=parent_descriptor,
        )
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("run-account state write made no progress")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temp_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        try:
            os.fsync(parent_descriptor)
        except OSError:
            pass
    except AccountRunConfigError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise AccountRunConfigError(
            code,
            f"cannot publish run-account state artifact {name}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(parent_descriptor)


def _read_regular_file_at_chain(
    *,
    base: Path,
    components: tuple[str, ...],
    name: str,
    code: str,
) -> bytes:
    descriptor = _open_directory_chain(
        base=base,
        components=components,
        create=False,
        code=code,
    )
    try:
        return _read_regular_file_at(descriptor, name, code=code)
    finally:
        os.close(descriptor)


def _read_regular_file_at(parent_descriptor: int, name: str, *, code: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AccountRunConfigError(
                code,
                f"account config artifact is not a regular file: {name}",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except AccountRunConfigError:
        raise
    except OSError as exc:
        raise AccountRunConfigError(
            code,
            f"account config artifact is unreadable: {name}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_directory_chain(
    *,
    base: Path,
    components: tuple[str, ...],
    create: bool,
    code: str,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(Path(base).resolve(), flags)
        for raw_component in components:
            component = _identity_component(raw_component, "path component")
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(
                component,
                flags | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except AccountRunConfigError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise AccountRunConfigError(
            code,
            "account config directory chain is unavailable or unsafe",
        ) from exc


def _decode_account_run_config(
    payload: bytes,
    *,
    expected_account: str,
) -> dict[str, Any]:
    try:
        config = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PAYLOAD_INVALID",
            "published account config is not readable JSON",
        ) from exc
    if not isinstance(config, dict):
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PAYLOAD_INVALID",
            "published account config must be a JSON object",
        )
    portfolio = config.get("portfolio")
    configured = portfolio.get("account") if isinstance(portfolio, dict) else None
    try:
        configured_account = normalize_account_label(configured)
    except ValueError as exc:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_ACCOUNT_MISMATCH",
            "published account config is not scoped to the requested account",
        ) from exc
    if configured_account != expected_account:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_ACCOUNT_MISMATCH",
            "published account config is not scoped to the requested account",
        )
    return config


def _absolute_without_symlink_resolution(path: Path) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_PATH_MISMATCH",
            "account config authority paths must be absolute",
        )
    return Path(os.path.abspath(str(raw)))


def _identity_component(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or Path(text).name != text
        or "/" in text
        or "\\" in text
    ):
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_IDENTITY_INVALID",
            f"{field_name} is not a safe path component",
        )
    return text


def _account_identity_component(value: str) -> str:
    try:
        return normalize_account_label(value)
    except ValueError as exc:
        raise AccountRunConfigError(
            "ACCOUNT_CONFIG_IDENTITY_INVALID",
            "account is not a safe canonical label",
        ) from exc


def prepare_tick_run_workspace(
    *,
    base: Path,
    run_id: str,
    default_account: str,
) -> TickRunWorkspace:
    run_id_norm = _identity_component(run_id, "run_id")
    default_account_norm = _account_identity_component(default_account)
    base_resolved = Path(base).resolve()
    for components in (
        ("output_accounts", default_account_norm),
        ("output_runs", run_id_norm, "required_data", "raw"),
        ("output_runs", run_id_norm, "required_data", "parsed"),
        ("output_runs", run_id_norm, "state"),
    ):
        descriptor = _open_directory_chain(
            base=base_resolved,
            components=components,
            create=True,
            code="ACCOUNT_CONFIG_PATH_UNSAFE",
        )
        os.close(descriptor)
    accounts_root = base_resolved / "output_accounts"
    ensure_account_output_dir(accounts_root / default_account_norm)

    run_dir = run_repo.ensure_run_dir(base_resolved, run_id_norm)
    required_dir = run_dir / "required_data"

    run_repo.ensure_run_state_dir(base_resolved, run_id_norm)
    try:
        state_repo.write_last_run_dir_pointer(base_resolved, run_id_norm)
    except Exception:
        pass

    return TickRunWorkspace(
        accounts_root=accounts_root,
        run_dir=run_dir,
        shared_required=required_dir,
    )
