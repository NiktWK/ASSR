"""Start / inspect / cancel an orchestrator run.

Shared by the Telegram bot and the secretary tool. One active run at a time.
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import team_db

PROJECT = Path(__file__).resolve().parent
RUN_DIR = PROJECT / "run"
PID_FILE = RUN_DIR / "orchestrator.pid"
CANCEL_FILE = RUN_DIR / "cancel"
LOCK_FILE = RUN_DIR / "start.lock"
CONSOLE_FILE = RUN_DIR / "console.log"


class RunBusy(Exception):
    """A run is already active."""


class RunError(Exception):
    """A run could not be started."""


def _pid() -> int | None:
    try:
        return int(PID_FILE.read_text("utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_is_orchestrator(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return True
    return "team.py" in cmdline and "run" in cmdline


def is_active() -> bool:
    pid = _pid()
    return bool(pid and pid > 0 and _pid_is_orchestrator(pid))


def run_tag() -> str:
    try:
        conn = team_db.connect()
    except Exception:
        return ""
    try:
        return team_db.get_meta(conn, "run_tag", "") or ""
    finally:
        conn.close()


def start_run(task: str) -> dict:
    task = (task or "").strip()
    if not task:
        raise RunError("пустая задача")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if is_active():
            raise RunBusy("прогон уже выполняется")
        with open(CONSOLE_FILE, "ab") as console:
            console.write(
                f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start: {task}\n".encode()
            )
            console.flush()
            proc = subprocess.Popen(
                [sys.executable, str(PROJECT / "team.py"), "run", task],
                cwd=str(PROJECT),
                stdout=console,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.time() + 20
        while time.time() < deadline:
            if is_active():
                return {"pid": proc.pid, "run_tag": run_tag()}
            if proc.poll() is not None:
                tail = _log_tail()
                raise RunError(
                    f"оркестратор завершился при старте (код {proc.returncode}).\n{tail}"
                )
            time.sleep(0.3)
        raise RunError("оркестратор не стартовал за 20 секунд; смотри run/console.log")


def _log_tail(limit: int = 2000) -> str:
    try:
        return CONSOLE_FILE.read_text("utf-8", "replace")[-limit:]
    except OSError:
        return ""


def _abort_all_sessions() -> None:
    try:
        import team  # lazy import to avoid a cycle at module load
    except Exception:
        return
    server = team.load_server()
    if not server:
        return
    client = team.OpenCode(server["url"], server.get("password"), timeout=30)
    conn = team_db.connect()
    try:
        for role in team_db.ROLES:
            session_id = team_db.get_session_id(conn, role)
            if session_id:
                client.abort(session_id)
    finally:
        conn.close()


def cancel_run() -> bool:
    """Ask the orchestrator to stop. Returns False when nothing was running."""
    if not is_active():
        return False
    CANCEL_FILE.write_text(str(time.time()), "utf-8")
    _abort_all_sessions()
    deadline = time.time() + 15
    while time.time() < deadline:
        if not is_active():
            _mark_cancelled()
            return True
        time.sleep(0.3)
    pid = _pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(1)
        if is_active():
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    _mark_cancelled()
    return True


def _mark_cancelled() -> None:
    try:
        conn = team_db.connect()
    except Exception:
        return
    try:
        if team_db.get_meta(conn, "run_status", "") in ("running", None):
            team_db.set_meta(conn, "run_status", "cancelled")
    finally:
        conn.close()