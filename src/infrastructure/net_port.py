"""Cheap TCP reachability probe (stdlib only, no intra-repo imports).

Kept separate so ``futu_gateway`` can import it without creating a static
import cycle with ``opend_watchdog`` (which imports futu_gateway lazily).
"""

from __future__ import annotations

import socket


def port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False
