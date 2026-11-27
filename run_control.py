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
RUNS_DIR = RUN_DIR / "runs"
PID_FILE = RUN_DIR / "orchestrator.pid"
CANCEL_FILE = RUN_DIR / "cancel"
LOCK_FILE = RUN_DIR / "start.lock"
CONSOLE_FILE = RUN_DIR / "console.log"  # legacy location; new runs use run/runs/<tag>/


def target_run_dir(resume_tag: str | None = None) -> Path:
    """Directory of the run to (re)start: explicit tag, else current pointer/legacy."""
    if resume_tag:
        return RUNS_DIR / resume_tag
    return team_db.current_run_dir()


def _console_file(run_dir: Path) -> Path:
    return run_dir / "console.log"


def _meta_run_tag(run_dir: Path) -> str:
    try:
        conn = team_db.connect(run_dir / "state.db")
        try:
            return team_db.get_meta(conn, "run_tag", "") or ""
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return ""


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


def start_run(task: str, resume: bool = False, resume_tag: str | None = None) -> dict:
    task = (task or "").strip()
    if not resume and not task:
        raise RunError("пустая задача")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if is_active():
            raise RunBusy("прогон уже выполняется")
        if resume:
            run_dir = target_run_dir(resume_tag)
            if not (run_dir / "state.db").exists():
                raise RunError("нечего продолжать: сохранённый прогон не найден")
            run_tag = resume_tag or _meta_run_tag(run_dir) or run_dir.name
        else:
            run_tag = time.strftime("%Y%m%d-%H%M%S")
            run_dir = RUNS_DIR / run_tag
        run_dir.mkdir(parents=True, exist_ok=True)
        console_path = _console_file(run_dir)
        with open(console_path, "ab") as console:
            label = "resume" if resume else "start"
            console.write(
                f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {label}: {task or '(saved run)'}\n".encode()
            )
            console.flush()
            command = [sys.executable, str(PROJECT / "team.py"), "run"]
            if resume:
                command.append("--resume")
                if resume_tag:
                    command.append(resume_tag)
            else:
                command += ["--fresh", "--run-tag", run_tag, task]
            proc = subprocess.Popen(
                command,
                cwd=str(PROJECT),
                stdout=console,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.time() + 20
        while time.time() < deadline:
            if is_active():
                return {"pid": proc.pid, "run_tag": run_tag, "run_dir": str(run_dir)}
            if proc.poll() is not None:
                tail = _log_tail(path=console_path)
                raise RunError(
                    f"оркестратор завершился при старте (код {proc.returncode}).\n{tail}"
                )
            time.sleep(0.3)
        raise RunError(f"оркестратор не стартовал за 20 секунд; смотри {console_path}")


def resume_run(run_tag: str | None = None) -> dict:
    """Continue a saved run (default: the current one). Same sessions, no reset."""
    return start_run("", resume=True, resume_tag=run_tag)


def _log_tail(limit: int = 2000, path: Path | None = None) -> str:
    try:
        return (path or CONSOLE_FILE).read_text("utf-8", "replace")[-limit:]
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