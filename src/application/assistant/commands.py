from __future__ import annotations

# Compatibility facade for the OM Copilot capability catalog.
#
# New code should treat capability_catalog as the authority for Copilot
# capabilities. This module remains as the slash-command/legacy import facade
# while callers are migrated.

from src.application.assistant.capability_catalog import *  # noqa: F401,F403
