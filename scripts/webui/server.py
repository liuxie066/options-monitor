from __future__ import annotations

import sys

from src.interfaces.webui import server as _impl
from src.interfaces.webui.server import *  # noqa: F401,F403


sys.modules[__name__] = _impl
