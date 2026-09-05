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

    def mark_failed(self, document_id: int, error: str, at: str | None = None) -> None:
        """Документ помнит свой сбой: иначе «упал» и «ещё не брали» неразличимы."""
        self.conn.execute(
            "UPDATE documents SET attempts = attempts + 1, last_error = ?, last_attempt_at = ? "
            "WHERE id = ?",
            ((error or "")[:500], at or _now_iso(), document_id),
        )

    def clear_failure(self, document_id: int, at: str | None = None) -> None:
        """Успешный прогон снимает отметку о сбое, счётчик попыток остаётся историей."""
        self.conn.execute(
            "UPDATE documents SET last_error = '', last_attempt_at = ? WHERE id = ?",
            (at or _now_iso(), document_id),
        )

    def count_failed(self) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM documents d LEFT JOIN item_sources s "
                "ON s.document_id = d.id "
                "WHERE s.document_id IS NULL AND d.hidden = 0 AND d.last_error <> ''"
            ).fetchone()[0]
        )

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
        only_failed: bool = False,
        category_weights: dict | None = None,
    ) -> list[sqlite3.Row]:
        """Documents that have no card yet (with --force: everything in the window).

        Порядок учитывает категорию источника: при очереди в тысячи документов
        НПА от регулятора не должен ждать за лентой СМИ (`category_weights`).
        `only_failed` оставляет только те, что упали в прошлый прогон.
        """
        where = ["d.hidden = 0"]
        params: list = []
        if not force:
            where.append("s.document_id IS NULL")
        if only_failed:
            where.append("d.last_error <> ''")
        if source_id is not None:
            where.append("d.source_id = ?")
            params.append(source_id)
        if since:
            where.append("(d.published_at >= ? OR d.published_at IS NULL)")
            params.append(since)
        weight, weight_params = _category_rank(category_weights)
        params.extend(weight_params)
        params.append(limit)
        return list(
            self.conn.execute(
                "SELECT d.* FROM documents d "
                "LEFT JOIN item_sources s ON s.document_id = d.id "
                "JOIN sources src ON src.id = d.source_id "
                "WHERE " + " AND ".join(where) + " "
                f"ORDER BY {weight} DESC, d.published_at DESC, d.id DESC LIMIT ?",
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


DEFAULT_CATEGORY_WEIGHTS = {"regulator": 3, "telegram": 2, "media": 1, "manual": 1}


def _category_rank(weights: dict | None) -> tuple[str, list]:
    """`CASE` по категории источника — вес приходит из конфигурации, не из кода."""
    table = weights if weights is not None else DEFAULT_CATEGORY_WEIGHTS
    if not table:
        # Не «0»: голое целое в ORDER BY SQLite читает как номер колонки.
        return "NULL", []
    branches = " ".join("WHEN ? THEN ?" for _ in table)
    params: list = []
    for category, weight in table.items():
        params.extend([category, int(weight)])
    return f"CASE src.category {branches} ELSE 0 END", params
