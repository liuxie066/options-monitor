"""Explicit foreground receiver only; never imported by the business service."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.application.trades.owner_diagnostic import receive_main

if __name__ == "__main__":
    raise SystemExit(receive_main())
