"""State storage for the AutomateML four-agent team (sqlite, stdlib only).

Single source of truth shared by the orchestrator (team.py) and the tool backend
(team_agent.py). Orchestration only: this module stores facts and messages, it never
judges the content of the agents' work.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

ROLES = ("controller", "researcher", "developer", "tester")
PHASES = ("plan", "build")
SUPERVISOR = {
    "developer": "researcher",
    "tester": "researcher",
    "researcher": "controller",
    "controller": "human",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    role      TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    phase     TEXT,
    submitted INTEGER NOT NULL DEFAULT 0,
    routed_phase TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    role     TEXT NOT NULL,
    action   TEXT NOT NULL,
    payload  TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
    id       TEXT PRIMARY KEY,
    worker   TEXT NOT NULL,
    goal     TEXT NOT NULL,
    context  TEXT,
    status   TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    from_role TEXT NOT NULL,
    to_role   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    ref       TEXT,
    phase     TEXT,
    text      TEXT NOT NULL,
    answer    TEXT,
    status    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   REAL NOT NULL,
    role TEXT NOT NULL,
    url  TEXT NOT NULL,
    note TEXT
);
"""


def db_path() -> Path:
    """Resolve the state database path (TEAM_DB env wins, else ./run/state.db)."""
    env = os.environ.get("TEAM_DB")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parent / "run" / "state.db")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init(path: Path | None = None) -> None:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(p)
    try:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "routed_phase" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN routed_phase TEXT")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- meta / sessions

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_session(conn: sqlite3.Connection, role: str, session_id: str) -> None:
    conn.execute(
        "INSERT INTO sessions(role, session_id, phase, submitted) VALUES(?, ?, NULL, 0) "
        "ON CONFLICT(role) DO UPDATE SET session_id=excluded.session_id, phase=NULL, "
        "submitted=0, routed_phase=NULL",
        (role, session_id),
    )
    conn.commit()


def get_session_id(conn: sqlite3.Connection, role: str) -> str | None:
    row = conn.execute("SELECT session_id FROM sessions WHERE role=?", (role,)).fetchone()
    return row["session_id"] if row else None


def role_of_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute(
        "SELECT role FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return row["role"] if row else None


def set_phase(conn: sqlite3.Connection, role: str, phase: str) -> None:
    """Start a new attempt of the phase: clear submitted/routed flags."""
    conn.execute(
        "UPDATE sessions SET phase=?, submitted=0, routed_phase=NULL WHERE role=?",
        (phase, role),
    )
    conn.commit()


def ensure_phase(conn: sqlite3.Connection, role: str, phase: str) -> None:
    """Enter the phase only if it changed; keep submitted/routed flags within a phase."""
    row = conn.execute(
        "SELECT phase FROM sessions WHERE role=?", (role,)
    ).fetchone()
    if row is None:
        return
    if row["phase"] != phase:
        conn.execute(
            "UPDATE sessions SET phase=?, submitted=0, routed_phase=NULL WHERE role=?",
            (phase, role),
        )
        conn.commit()


def get_phase(conn: sqlite3.Connection, role: str) -> str | None:
    row = conn.execute("SELECT phase FROM sessions WHERE role=?", (role,)).fetchone()
    return row["phase"] if row else None


def mark_submitted(conn: sqlite3.Connection, role: str) -> None:
    conn.execute("UPDATE sessions SET submitted=1 WHERE role=?", (role,))
    conn.commit()


def is_submitted(conn: sqlite3.Connection, role: str) -> bool:
    row = conn.execute("SELECT submitted FROM sessions WHERE role=?", (role,)).fetchone()
    return bool(row and row["submitted"])


def mark_routed(conn: sqlite3.Connection, role: str, phase: str) -> None:
    conn.execute("UPDATE sessions SET routed_phase=? WHERE role=?", (phase, role))
    conn.commit()


def is_routed(conn: sqlite3.Connection, role: str, phase: str) -> bool:
    row = conn.execute("SELECT routed_phase FROM sessions WHERE role=?", (role,)).fetchone()
    return bool(row and row["routed_phase"] == phase)


# ------------------------------------------------------------------------- events

def add_event(conn: sqlite3.Connection, role: str, action: str, payload: dict) -> int:
    cur = conn.execute(
        "INSERT INTO events(ts, role, action, payload) VALUES(?, ?, ?, ?)",
        (time.time(), role, action, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def next_event(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM events WHERE consumed=0 ORDER BY id LIMIT 1"
    ).fetchone()


def consume_event(conn: sqlite3.Connection, event_id: int) -> None:
    conn.execute("UPDATE events SET consumed=1 WHERE id=?", (event_id,))
    conn.commit()


def has_pending_submit(
    conn: sqlite3.Connection, role: str, phase: str, after_id: int
) -> bool:
    for row in conn.execute(
        "SELECT payload FROM events WHERE consumed=0 AND role=? AND action='submit' AND id>?",
        (role, after_id),
    ):
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            continue
        if payload.get("phase") == phase:
            return True
    return False


def recent_events(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()[::-1]


# -------------------------------------------------------------------------- tasks

def new_task(conn: sqlite3.Connection, worker: str, goal: str, context: str = "") -> str:
    seq = int(get_meta(conn, "task_seq", "0")) + 1
    set_meta(conn, "task_seq", str(seq))
    tid = f"t{seq}"
    conn.execute(
        "INSERT INTO tasks(id, worker, goal, context, status, attempts, created) "
        "VALUES(?, ?, ?, ?, 'plan_pending', 0, ?)",
        (tid, worker, goal, context, time.time()),
    )
    conn.commit()
    return tid


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def set_task_status(conn: sqlite3.Connection, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    conn.commit()


def bump_attempts(conn: sqlite3.Connection, task_id: str) -> int:
    conn.execute("UPDATE tasks SET attempts=attempts+1 WHERE id=?", (task_id,))
    conn.commit()
    row = get_task(conn, task_id)
    return int(row["attempts"]) if row else 0


def active_task_for(conn: sqlite3.Connection, worker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tasks WHERE worker=? AND status != 'done' ORDER BY created LIMIT 1",
        (worker,),
    ).fetchone()


def list_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM tasks ORDER BY created").fetchall()


# ---------------------------------------------------------------------- questions

def new_question(
    conn: sqlite3.Connection,
    from_role: str,
    to_role: str,
    kind: str,
    text: str,
    ref: str | None = None,
    phase: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO questions(ts, from_role, to_role, kind, ref, phase, text, status) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, 'open')",
        (time.time(), from_role, to_role, kind, ref, phase, text),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_question(conn: sqlite3.Connection, qid) -> sqlite3.Row | None:
    try:
        ident = int(str(qid).lstrip("qQ"))
    except ValueError:
        return None
    return conn.execute("SELECT * FROM questions WHERE id=?", (ident,)).fetchone()


def open_questions(conn: sqlite3.Connection, to_role: str | None = None) -> list[sqlite3.Row]:
    if to_role:
        return conn.execute(
            "SELECT * FROM questions WHERE status='open' AND to_role=? ORDER BY id",
            (to_role,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM questions WHERE status='open' ORDER BY id"
    ).fetchall()


def answer_question(conn: sqlite3.Connection, qid, text: str) -> None:
    ident = int(str(qid).lstrip("qQ"))
    conn.execute(
        "UPDATE questions SET answer=?, status='answered' WHERE id=?", (text, ident)
    )
    conn.commit()


def answer_human(conn: sqlite3.Connection, qid, text: str) -> int:
    """Answer an open human question and queue the answer event for the orchestrator."""
    question = get_question(conn, qid)
    if not question:
        raise ValueError(f"вопрос {qid} не найден")
    if question["to_role"] != "human":
        raise ValueError(f"вопрос q{question['id']} адресован не человеку")
    if question["status"] != "open":
        raise ValueError(f"вопрос q{question['id']} уже закрыт")
    if not (text or "").strip():
        raise ValueError("пустой ответ")
    answer_question(conn, question["id"], text.strip())
    add_event(
        conn,
        "human",
        "answer",
        {"question_id": int(question["id"]), "text": text.strip(), "from_role": "human"},
    )
    return int(question["id"])


# ------------------------------------------------------------------------ sources

def add_source(conn: sqlite3.Connection, role: str, url: str, note: str) -> int:
    cur = conn.execute(
        "INSERT INTO sources(ts, role, url, note) VALUES(?, ?, ?, ?)",
        (time.time(), role, url, note),
    )
    conn.commit()
    return int(cur.lastrowid)


def last_final_result(conn: sqlite3.Connection) -> str | None:
    """Latest controller BUILD submission (the final conclusion), if any."""
    row = conn.execute(
        "SELECT payload FROM events WHERE role='controller' AND action='submit' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    if payload.get("phase") != "build":
        return None
    return payload.get("content") or None


def format_event_meta(event) -> str:
    """Metadata-only one-line event rendering: never includes message text."""
    try:
        payload = json.loads(event["payload"])
    except (KeyError, json.JSONDecodeError):
        payload = {}
    action = event["action"]
    detail = {
        "submit": lambda p: f'phase={p.get("phase")} task={p.get("task_id")}',
        "review": lambda p: (
            f'phase={p.get("phase")} task={p.get("task_id")} verdict={p.get("verdict")}'
        ),
        "assign": lambda p: f'task={p.get("task_id")} worker={p.get("worker")}',
        "ask": lambda p: f'q{p.get("question_id")} → {p.get("to_role")}',
        "answer": lambda p: f'q{p.get("question_id")} от {p.get("from_role")}',
        "sources": lambda p: p.get("url", ""),
        "inject": lambda p: f'→ {p.get("target", "controller")}',
    }.get(action, lambda p: "")
    return (
        f'#{event["id"]:<4} {time.strftime("%H:%M:%S", time.localtime(event["ts"]))} '
        f'{event["role"]:<10} {action:<8} {detail(payload)}'
    )


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM sources ORDER BY id").fetchall()


def summary(conn: sqlite3.Connection) -> str:
    lines = ["Состояние команды:"]
    for row in conn.execute("SELECT role, phase, submitted FROM sessions"):
        lines.append(
            f"- {row['role']}: фаза={row['phase']}, сдано={bool(row['submitted'])}"
        )
    tasks = list_tasks(conn)
    if tasks:
        lines.append("Задачи:")
        for t in tasks:
            lines.append(f"- {t['id']} ({t['worker']}): {t['status']} — {t['goal'][:80]}")
    qs = open_questions(conn)
    if qs:
        lines.append("Открытые вопросы:")
        for q in qs:
            lines.append(f"- q{q['id']} ({q['from_role']}→{q['to_role']}): {q['text'][:80]}")
    return "\n".join(lines)
