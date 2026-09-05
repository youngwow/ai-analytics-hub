"""SQLite-репозиторий собранных документов."""

from __future__ import annotations

import sqlite3

from ..models import (
    RawDocument,
)
from ..utils import get_logger, to_utc_iso, utc_now
from .repository_interface import (
    DocumentRepository,
)

log = get_logger("db")


def _now_iso() -> str:
    return to_utc_iso(utc_now()) or ""


class SqliteDocumentRepository(DocumentRepository):
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def exists(self, source_id: int, external_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE source_id=? AND external_id=? LIMIT 1",
            (source_id, external_id),
        ).fetchone()
        return row is not None

    def find_by_url(self, url: str) -> int | None:
        row = self.conn.execute("SELECT id FROM documents WHERE url=? LIMIT 1", (url,)).fetchone()
        return int(row["id"]) if row else None

    def insert(self, doc: RawDocument) -> int:
        """Insert one document; the caller owns the transaction."""
        r = doc.to_row()
        cur = self.conn.execute(
            "INSERT INTO documents (source_id, external_id, url, title, summary, text, raw_html, "
            "author, attachments, published_at, fetched_at, content_hash, created_at) "
            "VALUES (:source_id, :external_id, :url, :title, :summary, :text, :raw_html, "
            ":author, :attachments, :published_at, :fetched_at, :content_hash, :created_at)",
            {**r, "created_at": _now_iso()},
        )
        return int(cur.lastrowid)

    def get(self, doc_id: int) -> RawDocument | None:
        row = self.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return RawDocument.from_row(row) if row else None

    def list(
        self, source_id: int | None = None, limit: int = 20, include_hidden: bool = False
    ) -> list[sqlite3.Row]:
        """Newest first (NULL dates last). Rows carry `source_name` and `text_len` for display."""
        where = [] if include_hidden else ["d.hidden = 0"]
        params: list = []
        if source_id is not None:
            where.append("d.source_id = ?")
            params.append(source_id)
        sql = (
            "SELECT d.id, d.source_id, s.name AS source_name, d.external_id, d.url, d.title, "
            "d.summary, d.author, d.attachments, d.published_at, d.fetched_at, d.content_hash, "
            "length(d.text) AS text_len "
            "FROM documents d JOIN sources s ON s.id = d.source_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY d.published_at DESC, d.id DESC LIMIT ?"
        )
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def count_unprocessed(self) -> int:
        """Очередь обработки: собрано, не скрыто и без карточки."""
        return int(
            self.conn.execute(
                "SELECT count(*) FROM documents d LEFT JOIN item_sources s "
                "ON s.document_id = d.id WHERE s.document_id IS NULL AND d.hidden = 0"
            ).fetchone()[0]
        )

    def count(self, source_id: int | None = None) -> int:
        if source_id is None:
            return int(self.conn.execute("SELECT count(*) FROM documents").fetchone()[0])
        return int(
            self.conn.execute(
                "SELECT count(*) FROM documents WHERE source_id=?", (source_id,)
            ).fetchone()[0]
        )


    def unprocessed(
        self,
        limit: int = 200,
        source_id: int | None = None,
        since: str | None = None,
        force: bool = False,
    ) -> list[sqlite3.Row]:
        """Documents that have no card yet (with --force: everything in the window)."""
        where = ["d.hidden = 0"]
        params: list = []
        if not force:
            where.append("s.document_id IS NULL")
        if source_id is not None:
            where.append("d.source_id = ?")
            params.append(source_id)
        if since:
            where.append("(d.published_at >= ? OR d.published_at IS NULL)")
            params.append(since)
        params.append(limit)
        return list(
            self.conn.execute(
                "SELECT d.* FROM documents d "
                "LEFT JOIN item_sources s ON s.document_id = d.id "
                "WHERE " + " AND ".join(where) + " "
                "ORDER BY d.published_at DESC, d.id DESC LIMIT ?",
                params,
            )
        )

    def set_derived(
        self, doc_id: int, *, simhash: str = "", embedding: bytes | None = None, norm_text: str = ""
    ) -> None:
        """Store what S0/S1 computed; the caller owns the transaction."""
        self.conn.execute(
            "UPDATE documents SET simhash=?, embedding=COALESCE(?, embedding), norm_text=? "
            "WHERE id=?",
            (simhash, embedding, norm_text, doc_id),
        )

    def clustered_candidates(self, since: str | None = None, limit: int = 2000) -> list[sqlite3.Row]:
        """Already-carded documents a new one could join — the dedup blocking pool."""
        where = ["d.simhash <> ''"]
        params: list = []
        if since:
            where.append("(d.published_at >= ? OR d.published_at IS NULL)")
            params.append(since)
        params.append(limit)
        return list(
            self.conn.execute(
                "SELECT d.id, d.simhash, d.embedding, d.title, d.url, "
                "s.item_id, i.cluster_id, i.type, i.npa_key "
                "FROM documents d "
                "JOIN item_sources s ON s.document_id = d.id "
                "JOIN items i ON i.id = s.item_id "
                "WHERE " + " AND ".join(where) + " ORDER BY d.id DESC LIMIT ?",
                params,
            )
        )


DocumentRepo = SqliteDocumentRepository
