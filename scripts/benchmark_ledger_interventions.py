"""Run the offline synthetic ledger benchmark from its test support module."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.ledger_intervention_benchmark import main


if __name__ == "__main__":
    main()
