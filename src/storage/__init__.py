"""SQLite persistence for sources, documents and per-source fetch state."""

from .db import Database, DuplicateSourceError

__all__ = ["Database", "DuplicateSourceError"]
