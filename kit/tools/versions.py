"""Record-format identities; kit release version is stored in ../VERSION."""

LEGACY_FORMAT = "0.1.0"
CURRENT_FORMAT = "0.2.0"
FORMAT_ALIASES = {
    LEGACY_FORMAT: LEGACY_FORMAT,
    "0.1-p1": LEGACY_FORMAT,
    CURRENT_FORMAT: CURRENT_FORMAT,
    "0.1-p2": CURRENT_FORMAT,
}
SUPPORTED_FORMATS = ", ".join(FORMAT_ALIASES)


def record_format(value):
    """Resolve an exact known label without changing the input record."""
    return FORMAT_ALIASES.get(value) if isinstance(value, str) else None
