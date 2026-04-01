"""Logging configuration (Stage 1 infrastructure).

Keep it standard: use Python logging. Avoid bespoke global log() functions.
Later refactors will route existing prints/log() into this.
"""

from __future__ import annotations

import logging
import os


def get_logger(name: str = 'options-monitor') -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    level_name = os.environ.get('OM_LOG_LEVEL', 'INFO').upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)

    h = logging.StreamHandler()
    fmt = logging.Formatter('[%(levelname)s] %(name)s: %(message)s')
    h.setFormatter(fmt)
    logger.addHandler(h)
    logger.propagate = False
    return logger
