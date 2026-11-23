"""SQLite persistence. Three kinds of record:
  reminders   - one row per (member, event, stage): the *state* of each message (idempotency via UNIQUE)
  events      - append-only audit log: message, timestamp, delivery status, member id, response
  escalations - human follow-up tasks raised when the SLA is breached
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from .models import Member

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY, partner_id TEXT NOT NULL, phone TEXT NOT NULL,
    data TEXT NOT NULL, opted_out INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_members_phone ON members(phone);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id TEXT NOT NULL, event_key TEXT NOT NULL, stage TEXT NOT NULL, parent_id INTEGER,
    state TEXT NOT NULL, scheduled_for TEXT, message TEXT, generation_source TEXT,
    sent_at TEXT, provider_message_id TEXT, delivery_status TEXT, error TEXT, note TEXT,
    response TEXT, response_intent TEXT, responded_at TEXT, sla_due_at TEXT,
    UNIQUE (member_id, event_key, stage)
);
CREATE INDEX IF NOT EXISTS idx_reminders_sid ON reminders(provider_message_id);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(state, sla_due_at);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, member_id TEXT, reminder_id INTEGER,
    type TEXT NOT NULL, status TEXT, message TEXT, response TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_member ON events(member_id, event_id);

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id INTEGER PRIMARY KEY AUTOINCREMENT, member_id TEXT NOT NULL, reminder_id INTEGER,
    reason TEXT NOT NULL, actions TEXT NOT NULL, created_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0, resolution TEXT
);
"""

ACTIVE_STATES = ("awaiting_response", "followed_up")
_REMINDER_COLUMNS = {
    "state", "scheduled_for", "message", "generation_source", "sent_at", "provider_message_id",
    "delivery_status", "error", "note", "response", "response_intent", "responded_at", "sla_due_at",
}


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ---- low level
    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # ---- members
    def upsert_member(self, member: Member, now_iso: str) -> None:
        self._run(
            "INSERT INTO members(member_id, partner_id, phone, data, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(member_id) DO UPDATE SET partner_id=excluded.partner_id, phone=excluded.phone, "
            "data=excluded.data, updated_at=excluded.updated_at",
            (member.member_id, member.partner_id, member.phone, member.model_dump_json(), now_iso),
        )

    def get_member(self, member_id: str) -> Member | None:
        row = self._one("SELECT data FROM members WHERE member_id=?", (member_id,))
        return Member.model_validate_json(row["data"]) if row else None

    def list_members(self) -> list[Member]:
        return [Member.model_validate_json(r["data"]) for r in self._all("SELECT data FROM members ORDER BY member_id")]

    def find_members_by_phone(self, phone: str) -> list[Member]:
        rows = self._all("SELECT data FROM members WHERE phone=? ORDER BY updated_at DESC", (phone,))
        return [Member.model_validate_json(r["data"]) for r in rows]

    def is_opted_out(self, member_id: str) -> bool:
        row = self._one("SELECT opted_out FROM members WHERE member_id=?", (member_id,))
        return bool(row and row["opted_out"])

    def set_opt_out(self, member_id: str, value: bool = True) -> None:
        self._run("UPDATE members SET opted_out=? WHERE member_id=?", (int(value), member_id))

    # ---- reminders
    def create_reminder(self, *, member_id: str, event_key: str, stage: str, state: str,
                        scheduled_for: str | None = None, parent_id: int | None = None,
                        note: str | None = None) -> int | None:
        """Returns the new id, or None if (member, event, stage) already exists (idempotency guard)."""
        try:
            cur = self._run(
                "INSERT INTO reminders(member_id, event_key, stage, parent_id, state, scheduled_for, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (member_id, event_key, stage, parent_id, state, scheduled_for, note),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def update_reminder(self, reminder_id: int, **fields: Any) -> None:
        bad = set(fields) - _REMINDER_COLUMNS
        if bad:
            raise ValueError(f"unknown reminder columns: {bad}")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE reminders SET {sets} WHERE reminder_id=?", (*fields.values(), reminder_id))

    def get_reminder(self, reminder_id: int) -> dict | None:
        return self._one("SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,))

    def find_reminder_by_sid(self, sid: str) -> dict | None:
        return self._one("SELECT * FROM reminders WHERE provider_message_id=?", (sid,))

    def handled_stages(self, member_id: str, event_key: str) -> set[str]:
        rows = self._all("SELECT stage FROM reminders WHERE member_id=? AND event_key=?", (member_id, event_key))
        return {r["stage"] for r in rows}

    def count_followups(self, member_id: str, event_key: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM reminders WHERE member_id=? AND event_key=? "
                        "AND substr(stage,1,9)='followup_'", (member_id, event_key))
        return int(row["n"]) if row else 0

    def due_awaiting(self, now_iso: str) -> list[dict]:
        return self._all("SELECT * FROM reminders WHERE state='awaiting_response' AND sla_due_at <= ? "
                         "ORDER BY sla_due_at, reminder_id", (now_iso,))

    def open_reminders(self, member_id: str, event_key: str) -> list[dict]:
        marks = ",".join("?" * len(ACTIVE_STATES))
        return self._all(f"SELECT * FROM reminders WHERE member_id=? AND event_key=? AND state IN ({marks}) "
                         "ORDER BY reminder_id", (member_id, event_key, *ACTIVE_STATES))

    def escalated_unanswered(self, member_id: str, event_key: str) -> list[dict]:
        return self._all("SELECT * FROM reminders WHERE member_id=? AND event_key=? AND state='escalated' "
                         "AND response IS NULL AND message IS NOT NULL ORDER BY reminder_id", (member_id, event_key))

    def latest_sent_reminder(self, member_id: str, event_key: str) -> dict | None:
        return self._one("SELECT * FROM reminders WHERE member_id=? AND event_key=? AND message IS NOT NULL "
                         "ORDER BY reminder_id DESC LIMIT 1", (member_id, event_key))

    def cancel_active(self, member_id: str, note: str, *, except_event_key: str | None = None) -> int:
        marks = ",".join("?" * len(ACTIVE_STATES))
        sql = f"UPDATE reminders SET state='cancelled', note=? WHERE member_id=? AND state IN ({marks})"
        params: list[Any] = [note, member_id, *ACTIVE_STATES]
        if except_event_key:
            sql += " AND event_key != ?"
            params.append(except_event_key)
        return self._run(sql, tuple(params)).rowcount

    def has_response_intent(self, member_id: str, event_key: str, intent: str) -> bool:
        return self._one("SELECT 1 AS x FROM reminders WHERE member_id=? AND event_key=? AND response_intent=? LIMIT 1",
                         (member_id, event_key, intent)) is not None

    def pollable_reminders(self, since_iso: str, limit: int = 50) -> list[dict]:
        """Recently sent messages whose delivery status is not final yet (used to poll the provider)."""
        return self._all(
            "SELECT * FROM reminders WHERE provider_message_id IS NOT NULL AND sent_at >= ? "
            "AND COALESCE(delivery_status,'queued') NOT IN ('read','failed','undelivered') "
            "ORDER BY reminder_id DESC LIMIT ?", (since_iso, limit))

    def has_event(self, reminder_id: int, type: str) -> bool:
        return self._one("SELECT 1 AS x FROM events WHERE reminder_id=? AND type=? LIMIT 1",
                         (reminder_id, type)) is not None

    def list_reminders(self, member_id: str | None = None) -> list[dict]:
        if member_id:
            return self._all("SELECT * FROM reminders WHERE member_id=? ORDER BY reminder_id", (member_id,))
        return self._all("SELECT * FROM reminders ORDER BY reminder_id")

    # ---- audit log
    def log_event(self, *, ts: str, type: str, member_id: str | None = None, reminder_id: int | None = None,
                  status: str | None = None, message: str | None = None, response: str | None = None,
                  detail: dict | None = None) -> int:
        cur = self._run(
            "INSERT INTO events(ts, member_id, reminder_id, type, status, message, response, detail) VALUES (?,?,?,?,?,?,?,?)",
            (ts, member_id, reminder_id, type, status, message, response, json.dumps(detail or {}, default=str)),
        )
        return int(cur.lastrowid)

    def list_events(self, member_id: str | None = None, limit: int = 500, after_id: int = 0) -> list[dict]:
        sql, params = "SELECT * FROM events WHERE event_id > ?", [after_id]
        if member_id:
            sql += " AND member_id=?"
            params.append(member_id)
        sql += " ORDER BY event_id LIMIT ?"
        params.append(limit)
        rows = self._all(sql, tuple(params))
        for r in rows:
            r["detail"] = json.loads(r["detail"] or "{}")
        return rows

    # ---- escalations
    def create_escalation(self, *, member_id: str, reminder_id: int | None, reason: str, actions: list[str],
                          created_at: str) -> int:
        cur = self._run("INSERT INTO escalations(member_id, reminder_id, reason, actions, created_at) VALUES (?,?,?,?,?)",
                        (member_id, reminder_id, reason, json.dumps(actions), created_at))
        return int(cur.lastrowid)

    def has_open_escalation(self, member_id: str, event_key: str, reason: str) -> bool:
        return self._one(
            "SELECT 1 AS x FROM escalations e JOIN reminders r ON r.reminder_id = e.reminder_id "
            "WHERE e.member_id=? AND r.event_key=? AND e.reason=? AND e.resolved=0 LIMIT 1",
            (member_id, event_key, reason)) is not None

    def list_escalations(self, member_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM escalations", ()
        if member_id:
            sql, params = sql + " WHERE member_id=?", (member_id,)
        rows = self._all(sql + " ORDER BY escalation_id", params)
        for r in rows:
            r["actions"] = json.loads(r["actions"])
        return rows

    def resolve_escalations_for_reminder(self, reminder_id: int, resolution: str) -> int:
        return self._run("UPDATE escalations SET resolved=1, resolution=? WHERE reminder_id=? AND resolved=0",
                         (resolution, reminder_id)).rowcount
