"""Record-format identities; kit release version is stored in ../VERSION."""

LEGACY_FORMAT = "0.1.0"
CURRENT_FORMAT = "0.2.0"
TASK_FORMAT = "0.3.0"
FORMAT_ALIASES = {
    LEGACY_FORMAT: LEGACY_FORMAT,
    "0.1-p1": LEGACY_FORMAT,
    CURRENT_FORMAT: CURRENT_FORMAT,
    "0.1-p2": CURRENT_FORMAT,
    TASK_FORMAT: TASK_FORMAT,
}
STATE_FORMATS = ", ".join(label for label in FORMAT_ALIASES if label != TASK_FORMAT)


def record_format(value):
    """Resolve an exact known label without changing the input record."""
    return FORMAT_ALIASES.get(value) if isinstance(value, str) else None
