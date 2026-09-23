"""Raw fixture connections that participate in the current ledger writer protocol."""
import sqlite3


def connect_ledger_fixture(*args, **kwargs):
    conn = sqlite3.connect(*args, **kwargs)
    conn.create_function("om_trade_attribution_writer_v1", 0, lambda: 1)
    return conn
