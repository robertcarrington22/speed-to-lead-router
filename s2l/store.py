"""SQLite-backed store. One table of leads (JSON blob + indexed columns) and one of events."""
import json
import os
import sqlite3
import threading
import time
import uuid

DB_PATH = os.environ.get("S2L_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "s2l.sqlite"))

_lock = threading.RLock()
_conn = None


def _connect():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _conn.execute("PRAGMA journal_mode=WAL")  # readers and one writer coexist; the server and simulator share the file
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY,
                received_at REAL,
                source TEXT,
                email TEXT,
                tier TEXT,
                sla_result TEXT,
                body TEXT
            )"""
        )
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                lead_id TEXT,
                at REAL,
                type TEXT,
                detail TEXT
            )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS leads_received ON leads(received_at)")
        _conn.execute("CREATE INDEX IF NOT EXISTS leads_email ON leads(email)")
        _conn.execute("CREATE INDEX IF NOT EXISTS events_lead ON events(lead_id)")
        _conn.commit()
    return _conn


def reset():
    """Wipe everything. Used by the simulator's --fresh flag."""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM leads")
        c.execute("DELETE FROM events")
        c.commit()


def new_id(prefix="ld"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def insert_lead(lead):
    with _lock:
        c = _connect()
        c.execute(
            "INSERT INTO leads (id, received_at, source, email, tier, sla_result, body) VALUES (?,?,?,?,?,?,?)",
            (lead["id"], lead["received_at"], lead["source"], lead.get("email"), lead.get("tier"), lead.get("sla_result"), json.dumps(lead)),
        )
        c.commit()
    return lead


def update_lead(lead_id, **fields):
    with _lock:
        c = _connect()
        row = c.execute("SELECT body FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            return None
        lead = json.loads(row[0])
        lead.update(fields)
        c.execute(
            "UPDATE leads SET tier=?, sla_result=?, email=?, body=? WHERE id=?",
            (lead.get("tier"), lead.get("sla_result"), lead.get("email"), json.dumps(lead), lead_id),
        )
        c.commit()
        return lead


def get_lead(lead_id):
    with _lock:
        row = _connect().execute("SELECT body FROM leads WHERE id=?", (lead_id,)).fetchone()
    return json.loads(row[0]) if row else None


def find_recent_by_email(email, within_seconds=86400):
    if not email:
        return None
    cutoff = time.time() - within_seconds
    with _lock:
        row = _connect().execute(
            "SELECT body FROM leads WHERE email=? AND received_at>=? ORDER BY received_at DESC LIMIT 1",
            (email.lower(), cutoff),
        ).fetchone()
    return json.loads(row[0]) if row else None


def list_leads(since=None, limit=5000):
    with _lock:
        if since is None:
            rows = _connect().execute("SELECT body FROM leads ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = _connect().execute(
                "SELECT body FROM leads WHERE received_at>=? ORDER BY received_at DESC LIMIT ?", (since, limit)
            ).fetchall()
    return [json.loads(r[0]) for r in rows]


def add_event(lead_id, type_, detail="", at=None):
    ev = {"id": new_id("ev"), "lead_id": lead_id, "at": at or time.time(), "type": type_, "detail": detail}
    with _lock:
        c = _connect()
        c.execute("INSERT INTO events (id, lead_id, at, type, detail) VALUES (?,?,?,?,?)",
                  (ev["id"], lead_id, ev["at"], type_, detail))
        c.commit()
    return ev


def list_events(lead_id):
    with _lock:
        rows = _connect().execute(
            "SELECT id, lead_id, at, type, detail FROM events WHERE lead_id=? ORDER BY at", (lead_id,)
        ).fetchall()
    return [dict(id=r[0], lead_id=r[1], at=r[2], type=r[3], detail=r[4]) for r in rows]
