"""Known nonworking states in the trade listener status protocol."""

# Working stage labels include listening / starting / once. Recovery work can
# refresh the heartbeat while reconnecting, so freshness alone is insufficient.
# Consumers retain their own time windows, severity and legacy-label policies.
NONWORKING_LISTENER_STATUSES = frozenset({"blocked", "error", "reconnecting", "stopped"})
