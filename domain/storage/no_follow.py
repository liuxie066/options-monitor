"""Descriptor-relative writes beneath a trusted base directory."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from uuid import uuid4


class UnsafePathError(OSError):
    """A child path component is invalid or is not a real directory."""


def safe_component(value: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or Path(text).name != text or "/" in text or "\\" in text:
        raise UnsafePathError(f"unsafe path component: {text!r}")
    return text


def open_directory_chain(
    *,
    base: Path,
    components: tuple[str, ...],
    create: bool,
    final_mode: int | None = None,
) -> int:
    """Return an open directory fd without following child symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(Path(base).resolve(), flags)
        for index, raw_component in enumerate(components):
            component = safe_component(raw_component)
            if create:
                try:
                    os.mkdir(
                        component,
                        final_mode if index == len(components) - 1 and final_mode is not None else 0o755,
                        dir_fd=descriptor,
                    )
                except FileExistsError:
                    pass
            try:
                child = os.open(
                    component,
                    flags | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafePathError(f"unsafe directory component: {component}") from exc
                raise
            os.close(descriptor)
            descriptor = child
        if final_mode is not None:
            os.fchmod(descriptor, final_mode)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def atomic_replace_bytes(
    *,
    base: Path,
    components: tuple[str, ...],
    name: str,
    payload: bytes,
    file_mode: int | None = None,
    final_dir_mode: int | None = None,
) -> None:
    """Atomically replace a child file through a no-follow directory chain."""

    name = safe_component(name)
    parent = open_directory_chain(
        base=base,
        components=components,
        create=True,
        final_mode=final_dir_mode,
    )
    temp_name = f".{name}.{uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644 if file_mode is None else file_mode,
            dir_fd=parent,
        )
        if file_mode is not None:
            os.fchmod(descriptor, file_mode)
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
        os.replace(temp_name, name, src_dir_fd=parent, dst_dir_fd=parent)
        try:
            os.fsync(parent)
        except OSError:
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(parent)
