"""SQLite-репозитории обработки: прогоны сбора, профили компании, версии промптов, вызовы модели."""

from __future__ import annotations

import json
import sqlite3

from ..models import (
    CollectReport,
    CompanyProfile,
    LlmCall,
)
from ..utils import get_logger, to_utc_iso, utc_now

log = get_logger("db")


def _now_iso() -> str:
    return to_utc_iso(utc_now()) or ""


class RunRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, report: CollectReport) -> int:
        cur = self.conn.execute(
            "INSERT INTO collect_runs (started_at, finished_at, sources_ok, sources_fail, "
            "sources_not_modified, docs_new) VALUES (?, ?, ?, ?, ?, ?)",
            (
                report.started_at,
                report.finished_at,
                report.sources_ok,
                report.sources_fail,
                report.sources_not_modified,
                report.docs_new,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM collect_runs ORDER BY id DESC LIMIT 1").fetchone()


class ProfileRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, profile_id: int) -> CompanyProfile | None:
        row = self.conn.execute(
            "SELECT * FROM company_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        return CompanyProfile.from_row(row) if row else None

    def default(self) -> CompanyProfile | None:
        row = self.conn.execute(
            "SELECT * FROM company_profiles WHERE is_default=1 ORDER BY id LIMIT 1"
        ).fetchone()
        return CompanyProfile.from_row(row) if row else None

    def list(self) -> list[CompanyProfile]:
        return [
            CompanyProfile.from_row(r)
            for r in self.conn.execute("SELECT * FROM company_profiles ORDER BY id")
        ]

    def save(self, profile: CompanyProfile) -> CompanyProfile:
        """Insert or bump the version of an existing profile; never edits in place."""
        payload = json.dumps(profile.payload, ensure_ascii=False)
        existing = self.conn.execute(
            "SELECT * FROM company_profiles WHERE name=?", (profile.name,)
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                "INSERT INTO company_profiles (name, payload, version, is_default, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (profile.name, payload, 1, int(profile.is_default), _now_iso()),
            )
            self.conn.commit()
            profile.id, profile.version = int(cur.lastrowid), 1
            return profile
        version = (existing["version"] or 1) + 1
        self.conn.execute(
            "UPDATE company_profiles SET payload=?, version=?, updated_at=? WHERE id=?",
            (payload, version, _now_iso(), existing["id"]),
        )
        self.conn.commit()
        profile.id, profile.version = int(existing["id"]), version
        return profile

    def set_default(self, profile_id: int) -> bool:
        self.conn.execute("UPDATE company_profiles SET is_default=0")
        cur = self.conn.execute(
            "UPDATE company_profiles SET is_default=1 WHERE id=?", (profile_id,)
        )
        self.conn.commit()
        return cur.rowcount > 0


class PromptRepo:
    """Prompt templates by version: a card always says which one produced it."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ensure(self, stage: str, template: str, model: str, params: dict | None = None) -> int:
        payload = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
        row = self.conn.execute(
            "SELECT id FROM prompt_versions WHERE stage=? AND template=? AND model=? AND params=?",
            (stage, template, model, payload),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO prompt_versions (stage, template, model, params, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (stage, template, model, payload, _now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid)


class LlmCallRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, call: LlmCall) -> int:
        cur = self.conn.execute(
            "INSERT INTO llm_calls (item_id, stage, model, tokens_in, tokens_out, latency_ms, "
            "cost, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.item_id,
                call.stage,
                call.model,
                call.tokens_in,
                call.tokens_out,
                call.latency_ms,
                call.cost,
                call.status,
                call.error,
                call.created_at or _now_iso(),
            ),
        )
        return int(cur.lastrowid)

    def stats(self, since: str | None = None, until: str | None = None) -> sqlite3.Row:
        where, params = [], []
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if until:
            where.append("created_at <= ?")
            params.append(until)
        sql = (
            "SELECT count(*) AS calls, "
            "coalesce(avg(latency_ms), 0) AS avg_latency_ms, "
            "coalesce(sum(tokens_in), 0) AS tokens_in, "
            "coalesce(sum(tokens_out), 0) AS tokens_out, "
            "coalesce(sum(cost), 0) AS cost, "
            "sum(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed "
            "FROM llm_calls" + (" WHERE " + " AND ".join(where) if where else "")
        )
        return self.conn.execute(sql, params).fetchone()
