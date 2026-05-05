from __future__ import annotations

import sys

from src.application import agent_tool_init_local as _impl
from src.application.agent_tool_init_local import *  # noqa: F401,F403


sys.modules[__name__] = _impl
