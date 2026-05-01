from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    # Keep package import lightweight for test/runtime utilities that only need submodules.
    from .main import main as _main

    return _main(argv)


__all__ = ['main']
