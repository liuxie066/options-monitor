# Compatibility re-export after owner move

Pattern:

- Move the implementation to the new owner module.
- Leave the old module as a tiny re-export only when tests or external callers may still import it.
- Add a structural test that rejects core runtime imports from the old module and verifies the old file does not define the moved implementation.

Why It Works:

It removes duplicate business logic while avoiding an unnecessary all-at-once cleanup of historical test imports, docs, or operator-facing names.
