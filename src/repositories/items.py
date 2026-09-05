"""SQLite-репозитории карточек: кластеры, карточки, заметки, теги, поисковый индекс."""

from __future__ import annotations

import sqlite3
from typing import Iterable

from ..models import (
    Cluster,
    EntitySpan,
    Item,
    ItemNote,
    ItemRevision,
    ItemTag,
    NpaEvent,
)
from ..utils import get_logger, to_utc_iso, utc_now
from .repository_interface import (
    ItemRepository,
)

log = get_logger("db")


def _now_iso() -> str:
    return to_utc_iso(utc_now()) or ""


class ClusterRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, cluster: Cluster) -> int:
        """Insert a cluster; the caller owns the transaction."""
        cur = self.conn.execute(
            "INSERT INTO clusters (canonical_document_id, centroid_embedding, size, "
            "has_divergent_opinions, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                cluster.canonical_document_id,
                cluster.centroid_embedding,
                cluster.size,
                int(cluster.has_divergent_opinions),
                cluster.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def get(self, cluster_id: int) -> Cluster | None:
        row = self.conn.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        return Cluster.from_row(row) if row else None

    def grow(self, cluster_id: int, count: int = 1, *, divergent: bool | None = None) -> None:
        """`count` more publications joined; a repeated link passes 0 and changes nothing."""
        if count > 0:
            self.conn.execute(
                "UPDATE clusters SET size = size + ? WHERE id=?", (count, cluster_id)
            )
        if divergent:
            self.conn.execute(
                "UPDATE clusters SET has_divergent_opinions = 1 WHERE id=?", (cluster_id,)
            )


class SqliteItemRepository(ItemRepository):
    """Cards plus everything hanging off them: entities, sources, events, revisions."""

    _COLUMNS = (
        "cluster_id, type, npa_status, npa_key, title, summary, priority, relevance_score, "
        "reasoning, confidence, tags, analyst_note, visibility, hidden_reason, origin, "
        "is_archived, degraded, needs_review, date_estimated, model_name, prompt_version, "
        "profile_version, manual_overrides, processed_at, published_at"
    )

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, item: Item) -> int:
        row = item.to_row()
        placeholders = ", ".join(f":{c.strip()}" for c in self._COLUMNS.split(","))
        cur = self.conn.execute(
            f"INSERT INTO items ({self._COLUMNS}) VALUES ({placeholders})",
            {**row, "processed_at": row["processed_at"] or _now_iso()},
        )
        item.id = int(cur.lastrowid)
        return item.id

    def update(self, item: Item) -> None:
        row = item.to_row()
        assignments = ", ".join(f"{c.strip()}=:{c.strip()}" for c in self._COLUMNS.split(","))
        self.conn.execute(
            f"UPDATE items SET {assignments} WHERE id=:id", {**row, "id": item.id}
        )

    def get(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return Item.from_row(row) if row else None

    def by_cluster(self, cluster_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE cluster_id=?", (cluster_id,)).fetchone()
        return Item.from_row(row) if row else None

    def by_npa_key(self, npa_key: str) -> Item | None:
        """The live card for an act — the join point for every later publication about it."""
        row = self.conn.execute(
            "SELECT * FROM items WHERE npa_key=? AND is_archived=0 LIMIT 1", (npa_key,)
        ).fetchone()
        return Item.from_row(row) if row else None

    def list(
        self,
        *,
        type_: str | None = None,
        priority: str | None = None,
        tag: str | None = None,
        query: str | None = None,
        since: str | None = None,
        limit: int = 20,
        include_hidden: bool = False,
    ) -> list[sqlite3.Row]:
        """Feed rows, newest first; `query` goes through items_fts."""
        # Ленту очищает только `hidden_feed`; карточка, убранная из дайджеста,
        # остаётся видимой — иначе подготовка адресной выжимки чистит ленту всем
        # сразу (решение владельца от 2026-09-05).
        where: list[str] = (
            [] if include_hidden else ["i.visibility NOT IN ('hidden_feed', 'deleted')"]
        )
        params: list = []
        if type_:
            where.append("i.type = ?")
            params.append(type_)
        if priority:
            where.append("i.priority = ?")
            params.append(priority)
        if tag:
            where.append("EXISTS (SELECT 1 FROM json_each(i.tags) WHERE json_each.value = ?)")
            params.append(tag)
        if since:
            where.append("(i.published_at >= ? OR i.published_at IS NULL)")
            params.append(since)
        join = ""
        if query:
            join = "JOIN items_fts f ON f.rowid = i.id "
            where.append("items_fts MATCH ?")
            params.append(query)
        params.append(limit)
        sql = (
            "SELECT i.*, c.size AS sources_count FROM items i "
            "JOIN clusters c ON c.id = i.cluster_id " + join
            + ("WHERE " + " AND ".join(where) + " " if where else "")
            + "ORDER BY i.published_at DESC, i.id DESC LIMIT ?"
        )
        return list(self.conn.execute(sql, params))

    def count(self) -> int:
        return int(self.conn.execute("SELECT count(*) FROM items").fetchone()[0])

    # -- visibility: ничего не удаляется физически --

    def set_visibility(self, item_id: int, visibility: str, reason: str = "") -> bool:
        cur = self.conn.execute(
            "UPDATE items SET visibility=?, hidden_reason=? WHERE id=?",
            (visibility, reason, item_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_visibility_bulk(self, item_ids: Iterable[int], visibility: str, reason: str = "") -> int:
        """Массовая операция под дайджест: одна транзакция, отменяется целиком."""
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE items SET visibility=?, hidden_reason=? WHERE id IN ({marks})",
                [visibility, reason, *ids],
            )
        return cur.rowcount

    def hide_by_source(self, source_id: int) -> int:
        """`--purge-items`: карточки источника уходят из ленты, но остаются в базе."""
        cur = self.conn.execute(
            "UPDATE items SET visibility='hidden_feed', hidden_reason='источник удалён' "
            "WHERE visibility='visible' AND id IN ("
            "  SELECT s.item_id FROM item_sources s JOIN documents d ON d.id = s.document_id"
            "  WHERE d.source_id = ?)",
            (source_id,),
        )
        self.conn.commit()
        return cur.rowcount

    # -- entities --

    def add_entities(self, item_id: int, spans: Iterable[EntitySpan]) -> None:
        self.conn.executemany(
            "INSERT INTO entities (item_id, role, value, normalized_value, evidence_start, "
            "evidence_end) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (item_id, e.role, e.value, e.normalized_value, e.evidence_start, e.evidence_end)
                for e in spans
            ],
        )

    def entities(self, item_id: int) -> list[EntitySpan]:
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [EntitySpan.from_row(r) for r in rows]

    def clear_entities(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM entities WHERE item_id=?", (item_id,))

    # -- sources --

    def link_sources(
        self, item_id: int, document_ids: Iterable[int], canonical_id: int | None = None
    ) -> int:
        """Attach documents to a card; returns how many links are new (0 on a repeat)."""
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO item_sources (item_id, document_id, is_canonical) "
            "VALUES (?, ?, ?)",
            [(item_id, d, int(d == canonical_id)) for d in dict.fromkeys(document_ids)],
        )
        return max(cur.rowcount, 0)

    def item_for_document(self, document_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT item_id FROM item_sources WHERE document_id=? LIMIT 1", (document_id,)
        ).fetchone()
        return int(row["item_id"]) if row else None

    def sources(self, item_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT d.id, d.url, d.title, d.published_at, s.is_canonical, sr.name AS source_name "
                "FROM item_sources s JOIN documents d ON d.id = s.document_id "
                "JOIN sources sr ON sr.id = d.source_id "
                "WHERE s.item_id=? ORDER BY s.is_canonical DESC, d.id",
                (item_id,),
            )
        )

    # -- npa timeline --

    def add_event(self, event: NpaEvent) -> int:
        cur = self.conn.execute(
            "INSERT INTO npa_events (item_id, status, occurred_at, source_url, note, created_by, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.item_id,
                event.status,
                event.occurred_at,
                event.source_url,
                event.note,
                event.created_by,
                event.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def events(self, item_id: int) -> list[NpaEvent]:
        rows = self.conn.execute(
            "SELECT * FROM npa_events WHERE item_id=? ORDER BY occurred_at, id", (item_id,)
        )
        return [NpaEvent.from_row(r) for r in rows]

    def has_event(self, item_id: int, status: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM npa_events WHERE item_id=? AND status=? LIMIT 1", (item_id, status)
        ).fetchone()
        return row is not None

    # -- revisions --

    def add_revision(self, revision: ItemRevision) -> int:
        cur = self.conn.execute(
            "INSERT INTO item_revisions (item_id, field, old_value, new_value, actor, "
            "source_of_change, edit_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.item_id,
                revision.field,
                revision.old_value,
                revision.new_value,
                revision.actor,
                revision.source_of_change,
                revision.edit_reason,
                revision.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def revisions(self, item_id: int) -> list[ItemRevision]:
        rows = self.conn.execute(
            "SELECT * FROM item_revisions WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [ItemRevision.from_row(r) for r in rows]

    def last_model_value(self, item_id: int, field: str) -> ItemRevision | None:
        """The newest value the model proposed for a field — what `revert` restores."""
        row = self.conn.execute(
            "SELECT * FROM item_revisions WHERE item_id=? AND field=? AND source_of_change='llm' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (item_id, field),
        ).fetchone()
        return ItemRevision.from_row(row) if row else None

    def edited_share(self, since: str | None = None) -> float:
        """Share of cards an analyst touched — the honest proxy for model quality."""
        total = self.count()
        if not total:
            return 0.0
        sql = "SELECT count(*) FROM items WHERE manual_overrides <> '[]'"
        params: list = []
        if since:
            sql += " AND processed_at >= ?"
            params.append(since)
        edited = int(self.conn.execute(sql, params).fetchone()[0])
        return edited / total


class ItemNoteRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, note: ItemNote) -> int:
        cur = self.conn.execute(
            "INSERT INTO item_notes (item_id, body, author, created_at) VALUES (?, ?, ?, ?)",
            (note.item_id, note.body, note.author, note.created_at or _now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list(self, item_id: int) -> list[ItemNote]:
        rows = self.conn.execute(
            "SELECT * FROM item_notes WHERE item_id=? ORDER BY id", (item_id,)
        )
        return [ItemNote.from_row(r) for r in rows]


class ItemTagRepo:
    """Tags with provenance: reprocessing may only replace what the model put there."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def set_tags(self, item_id: int, tags: Iterable[str], *, is_manual: bool = False) -> None:
        """Replace tags of one origin; the other origin is left untouched."""
        self.conn.execute(
            "DELETE FROM item_tags WHERE item_id=? AND is_manual=?", (item_id, int(is_manual))
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO item_tags (item_id, tag, is_manual) VALUES (?, ?, ?)",
            [(item_id, t, int(is_manual)) for t in dict.fromkeys(tags)],
        )

    def list(self, item_id: int) -> list[ItemTag]:
        rows = self.conn.execute(
            "SELECT * FROM item_tags WHERE item_id=? ORDER BY is_manual DESC, tag", (item_id,)
        )
        return [ItemTag.from_row(r) for r in rows]

    def names(self, item_id: int) -> list[str]:
        return [t.tag for t in self.list(item_id)]


class SearchRepo:
    """Поисковый индекс: одна строка на карточку, шире самой карточки.

    Текст собирается из четырёх таблиц, поэтому строка перестраивается целиком
    (`DELETE` + `INSERT`), а не обновляется по частям: шесть триггеров с
    частичными обновлениями стоили бы дороже и разъезжались бы (research.md, R-01).
    """

    _BODY_SQL = """
        SELECT i.title, i.summary,
               (SELECT group_concat(t.tag, ' ') FROM item_tags t WHERE t.item_id = i.id) AS tags,
               (SELECT group_concat(e.value || ' ' || e.normalized_value, ' ')
                  FROM entities e WHERE e.item_id = i.id) AS entities,
               (SELECT d.norm_text FROM item_sources s JOIN documents d ON d.id = s.document_id
                 WHERE s.item_id = i.id ORDER BY s.is_canonical DESC, d.id LIMIT 1) AS body
          FROM items i WHERE i.id = ?
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def body_for(self, item_id: int) -> str:
        row = self.conn.execute(self._BODY_SQL, (item_id,)).fetchone()
        if row is None:
            return ""
        parts = [row["title"], row["summary"], row["tags"], row["entities"], row["body"]]
        return "\n".join(p for p in parts if p)

    def rebuild(self, item_id: int) -> None:
        """Пересобрать строку карточки; вызывается из той же транзакции, что и запись."""
        body = self.body_for(item_id)
        self.conn.execute("DELETE FROM items_search WHERE rowid = ?", (item_id,))
        if body.strip():
            self.conn.execute(
                "INSERT INTO items_search(rowid, body) VALUES (?, ?)", (item_id, body)
            )

    def remove(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM items_search WHERE rowid = ?", (item_id,))

    def rebuild_all(self) -> int:
        ids = [int(r["id"]) for r in self.conn.execute("SELECT id FROM items ORDER BY id")]
        for item_id in ids:
            self.rebuild(item_id)
        self.conn.commit()
        return len(ids)


ItemRepo = SqliteItemRepository
