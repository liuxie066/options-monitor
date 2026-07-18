"""options-monitor package namespace."""

import sys


if sys.version_info < (3, 12):
    observed = ".".join(str(part) for part in sys.version_info[:3])
    raise RuntimeError(
        f"options-monitor requires Python >= 3.12; executable={sys.executable}; observed={observed}. "
        "Recreate .venv with Python 3.12 or use a supported launcher with OM_PYTHON."
    )
