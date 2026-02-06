"""PocketBase-compatible ID generation.

PocketBase uses 15-character lowercase alphanumeric IDs.  We replicate this
using Python's ``secrets`` module for cryptographic randomness.
"""

from __future__ import annotations

import secrets

# PocketBase default alphabet: lowercase letters + digits
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
_ID_LENGTH = 15


def generate_id(length: int = _ID_LENGTH) -> str:
    """Generate a random alphanumeric ID.

    Args:
        length: Number of characters. Defaults to 15 (PocketBase compatible).

    Returns:
        A cryptographically random string composed of ``[a-z0-9]``.
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))
