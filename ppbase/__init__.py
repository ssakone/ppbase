"""PPBase -- a Python reimplementation of PocketBase using PostgreSQL."""

from __future__ import annotations

__version__ = "0.1.0"

from ppbase.ext.flask_like_pb import FlaskLikePB


class PPBase(FlaskLikePB):
    """Main PPBase facade."""


# Process-wide Flask-like singleton facade.
pb = PPBase()

__all__ = ["PPBase", "pb", "__version__"]
