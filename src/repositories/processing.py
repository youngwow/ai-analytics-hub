"""SQLite-репозитории обработки: прогоны сбора, профили компании, версии промптов, вызовы модели."""

from __future__ import annotations

import json
import sqlite3

from ..models import (
    CollectReport,
    CompanyProfile,
    LlmCall,
    ProcessingRun,
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


class ProcessingRunRepo:
    """История прогонов обработки: очередь ИИ видна и из CLI, и из дашборда."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start(self, params: dict, trigger: str = "cli", started_at: str | None = None) -> ProcessingRun:
        run = ProcessingRun(
            started_at=started_at or (to_utc_iso(utc_now()) or ""),
            status="running",
            trigger=trigger,
            params=dict(params),
        )
        cur = self.conn.execute(
            "INSERT INTO processing_runs (started_at, status, trigger, params) VALUES (?, ?, ?, ?)",
            (run.started_at, run.status, run.trigger, json.dumps(run.params, ensure_ascii=False)),
        )
        self.conn.commit()
        run.id = int(cur.lastrowid)
        return run

    def progress(self, run_id: int, counters: dict) -> None:
        """Publish committed results while a run is still executing.

        `processed` и `heartbeat_at` отвечают на два вопроса дашборда, на которые
        одних итоговых счётчиков не хватает: сколько из `documents` уже пройдено
        и жив ли прогон вообще (застрявший виден по возрасту биения).
        """
        fields = ("documents", "processed", "clusters", "items_new", "items_joined",
                  "items_updated", "degraded", "needs_review", "calls", "failed", "elapsed_s")
        self.conn.execute(
            "UPDATE processing_runs SET " + ", ".join(f"{name}=?" for name in fields)
            + ", heartbeat_at=? WHERE id=? AND status='running'",
            [counters.get(name, 0) for name in fields] + [to_utc_iso(utc_now()) or "", run_id],
        )
        self.conn.commit()

    def finish(self, run_id: int, counters: dict, finished_at: str | None = None) -> None:
        """Закрыть прогон отчётом: `counters` — поля `ProcessingReport` по именам."""
        self.conn.execute(
            "UPDATE processing_runs SET status='done', finished_at=?, processed=?, documents=?, "
            "clusters=?, items_new=?, items_joined=?, items_updated=?, degraded=?, needs_review=?, "
            "calls=?, failed=?, elapsed_s=? WHERE id=?",
            (
                finished_at or (to_utc_iso(utc_now()) or ""),
                int(counters.get("processed", counters.get("documents", 0))),
                int(counters.get("documents", 0)),
                int(counters.get("clusters", 0)),
                int(counters.get("items_new", 0)),
                int(counters.get("items_joined", 0)),
                int(counters.get("items_updated", 0)),
                int(counters.get("degraded", 0)),
                int(counters.get("needs_review", 0)),
                int(counters.get("calls", 0)),
                int(counters.get("failed", 0)),
                float(counters.get("elapsed_s", 0.0)),
                run_id,
            ),
        )
        self.conn.commit()

    def fail(self, run_id: int, error: str, finished_at: str | None = None) -> None:
        self.conn.execute(
            "UPDATE processing_runs SET status='failed', finished_at=?, error=? WHERE id=?",
            (finished_at or (to_utc_iso(utc_now()) or ""), error[:500], run_id),
        )
        self.conn.commit()

    def get(self, run_id: int) -> ProcessingRun | None:
        row = self.conn.execute("SELECT * FROM processing_runs WHERE id=?", (run_id,)).fetchone()
        return ProcessingRun.from_row(row) if row else None

    def running(self) -> ProcessingRun | None:
        row = self.conn.execute(
            "SELECT * FROM processing_runs WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return ProcessingRun.from_row(row) if row else None

    def latest(self) -> ProcessingRun | None:
        row = self.conn.execute("SELECT * FROM processing_runs ORDER BY id DESC LIMIT 1").fetchone()
        return ProcessingRun.from_row(row) if row else None

    def list(self, limit: int = 20) -> list[ProcessingRun]:
        rows = self.conn.execute(
            "SELECT * FROM processing_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [ProcessingRun.from_row(r) for r in rows]

    def abandon_running(self, error: str) -> int:
        """Прогон не переживает перезапуск процесса: всё «running» на старте — провал."""
        cur = self.conn.execute(
            "UPDATE processing_runs SET status='failed', finished_at=?, error=? WHERE status='running'",
            (to_utc_iso(utc_now()) or "", error[:500]),
        )
        self.conn.commit()
        return cur.rowcount


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

    def breakdown(self, since: str | None = None, until: str | None = None) -> list[dict]:
        """Вызовы модели по этапу и статусу: где именно теряется прогон."""
        where, params = [], []
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if until:
            where.append("created_at <= ?")
            params.append(until)
        sql = (
            "SELECT stage, status, count(*) AS calls, "
            "coalesce(avg(latency_ms), 0) AS avg_latency_ms, "
            "coalesce(sum(tokens_in), 0) AS tokens_in, "
            "coalesce(sum(tokens_out), 0) AS tokens_out "
            "FROM llm_calls "
            + ("WHERE " + " AND ".join(where) + " " if where else "")
            + "GROUP BY stage, status ORDER BY calls DESC"
        )
        return [
            {
                "stage": r["stage"],
                "status": r["status"],
                "calls": int(r["calls"]),
                "avg_latency_ms": int(r["avg_latency_ms"] or 0),
                "tokens_in": int(r["tokens_in"]),
                "tokens_out": int(r["tokens_out"]),
            }
            for r in self.conn.execute(sql, params)
        ]

    def by_day(self, since: str | None = None, until: str | None = None, limit: int = 14) -> list[dict]:
        """Сутки прогонов: сколько вызовов и токенов ушло по дням."""
        where, params = [], []
        if since:
            where.append("created_at >= ?")
            params.append(since)
        if until:
            where.append("created_at <= ?")
            params.append(until)
        sql = (
            "SELECT substr(created_at, 1, 10) AS day, count(*) AS calls, "
            "coalesce(sum(tokens_in), 0) AS tokens_in, "
            "coalesce(sum(tokens_out), 0) AS tokens_out, "
            "sum(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS failed "
            "FROM llm_calls "
            + ("WHERE " + " AND ".join(where) + " " if where else "")
            + "GROUP BY day ORDER BY day DESC LIMIT ?"
        )
        return [
            {
                "day": r["day"],
                "calls": int(r["calls"]),
                "tokens_in": int(r["tokens_in"]),
                "tokens_out": int(r["tokens_out"]),
                "failed": int(r["failed"] or 0),
            }
            for r in self.conn.execute(sql, [*params, limit])
        ]

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
