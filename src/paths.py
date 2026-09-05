"""Filesystem locations for the project, resolved once and injectable for tests."""

from __future__ import annotations

import os
from dataclasses import dataclass

# src/paths.py → src/ → project root
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.dirname(_PKG_DIR)


@dataclass(frozen=True)
class ProjectPaths:
    """Absolute paths the ingestion layer reads/writes.

    Defaults derive from the package location (works when run from the repo
    root). `HUB_ROOT` overrides the root; tests construct an instance pointing
    at a tmp dir instead of monkeypatching a global.
    """

    root: str
    data_dir: str
    config_path: str
    sources_path: str
    env_path: str

    @classmethod
    def from_root(cls, root: str | None = None) -> "ProjectPaths":
        root = root or os.environ.get("HUB_ROOT") or DEFAULT_ROOT
        return cls(
            root=root,
            data_dir=os.path.join(root, "data"),
            config_path=os.path.join(root, "config.yaml"),
            sources_path=os.path.join(root, "sources.json"),
            env_path=os.path.join(root, ".env"),
        )

    def data(self, *parts: str) -> str:
        """Join a path under data/ (e.g. paths.data('hub.db'))."""
        return os.path.join(self.data_dir, *parts)

    @property
    def db_path(self) -> str:
        return self.data("hub.db")


DEFAULT_PATHS = ProjectPaths.from_root()
