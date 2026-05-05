from __future__ import annotations

import sys

from src.application import agent_tool_handlers as _impl
from src.application.agent_tool_handlers import *  # noqa: F401,F403


sys.modules[__name__] = _impl
