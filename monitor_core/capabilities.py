"""Supported monitoring scopes and local data filenames."""

ALLOWED_TARGET_SCOPES = frozenset({"self", "party"})


def local_data_filename(filename: str) -> str:
    """Use the application's ordinary local filename."""
    return filename
