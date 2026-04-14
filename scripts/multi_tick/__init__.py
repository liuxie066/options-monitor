from __future__ import annotations


def main():
    # Keep package import lightweight for test/runtime utilities that only need submodules.
    from .main import main as _main

    return _main()


__all__ = ['main']
