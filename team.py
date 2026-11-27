#!/usr/bin/env python3
"""AutomateML team orchestrator.

Starts a headless opencode server, creates one long-lived session per role
(controller, researcher, developer, tester), and routes messages between them.

The orchestrator only routes: it never judges the content of the agents' work.
Agents hand in phase results through the `team` tool (see .opencode/tools/team.ts).

Usage:
    python3 team.py run "<задача>"        # start a run (interactive human channel)
    python3 team.py status                # state overview
    python3 team.py events [-n 30]        # recent events
    python3 team.py answer <qid> <text>   # answer a human question
    python3 team.py stop                  # stop the opencode server started by a run
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import team_db

PROJECT = Path(__file__).resolve().parent
RUN_DIR = PROJECT / "run"
SERVER_FILE = RUN_DIR / "server.json"
LOG_FILE = RUN_DIR / "events.log"
CHAT_DIR = RUN_DIR / "chat"
DB_PATH = RUN_DIR / "state.db"
PID_FILE = RUN_DIR / "orchestrator.pid"
CANCEL_FILE = RUN_DIR / "cancel"

ROLES = team_db.ROLES
ROLE_RU = {
    "controller": "Контролёр",
    "researcher": "Исследователь",
    "developer": "Разработчик",
    "tester": "Тестировщик",
}
APPROVAL_WORDS = {"approve", "accept", "ok", "ок", "да", "yes", "y", "принять", "принято"}


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def short(text: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_approval(text: str) -> bool:
    low = text.strip().lower()
    return low in APPROVAL_WORDS or low.startswith("approve")


# --------------------------------------------------------------------- opencode HTTP

class OpenCodeError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class RunCancelled(Exception):
    """The operator cancelled the run."""


class OpenCode:
    def __init__(self, base_url: str, password: str | None = None, timeout: int = 1800):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.password:
            token = base64.b64encode(f"opencode:{self.password}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        return headers

    def request(self, method: str, path: str, body=None, timeout: float | None = None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = Request(
            self.base_url + path, data=data, headers=self._headers(), method=method
        )
        try:
            with urlopen(req, timeout=self.timeout if timeout is None else timeout) as resp:
                raw = resp.read()
        except HTTPError as exc:
            raw = exc.read()
            raise OpenCodeError(
                f"HTTP {exc.code} {path}", status=exc.code, body=raw.decode("utf-8", "replace")
            ) from None
        except URLError as exc:
            raise OpenCodeError(f"сеть: {exc.reason}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")

    def health(self) -> bool:
        try:
            self.request("GET", "/global/health", timeout=3)
            return True
        except Exception:  # noqa: BLE001 - starting/stopped server, timeouts, auth
            return False

    def create_session(self, title: str) -> str:
        result = self.request("POST", "/session", {"title": title})
        session_id = (result or {}).get("id")
        if not session_id:
            raise OpenCodeError(f"не удалось создать сессию: {result!r}")
        return session_id

    @staticmethod
    def _signature(messages) -> tuple:
        total = 0
        text_len = 0
        for message in messages:
            parts = message.get("parts") or []
            total += len(parts)
            for part in parts:
                if isinstance(part, dict) and part.get("text"):
                    text_len += len(part["text"])
        return (len(messages), total, text_len)

    def prompt(
        self,
        session_id: str,
        agent: str,
        text: str,
        attempts: int = 3,
        stall_timeout: int = 300,
        model: dict | None = None,
    ):
        path = f"/session/{quote(session_id, safe='')}/message"
        body: dict = {"agent": agent, "parts": [{"type": "text", "text": text}]}
        if model:
            body["model"] = model
        last_error = None
        for attempt in range(1, attempts + 1):
            holder: dict = {}

            def worker() -> None:
                try:
                    holder["result"] = self.request("POST", path, body)
                except BaseException as exc:  # noqa: BLE001 - propagate to caller
                    holder["error"] = exc

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            signature = None
            last_change = time.time()
            stalled = False
            while thread.is_alive():
                thread.join(3)
                if not thread.is_alive():
                    break
                try:
                    sig = self._signature(self.messages(session_id))
                except Exception:  # noqa: BLE001 - watchdog is best effort
                    sig = signature
                if sig != signature:
                    signature = sig
                    last_change = time.time()
                elif stall_timeout and time.time() - last_change > stall_timeout:
                    stalled = True
                    self.abort(session_id)
                    break
            if stalled:
                raise OpenCodeError(
                    f"ход завис: нет активности {stall_timeout}s, сессия прервана"
                )
            if "error" in holder:
                exc = holder["error"]
                if isinstance(exc, OpenCodeError):
                    last_error = exc
                    if exc.status in (429, 500, 502, 503) and attempt < attempts:
                        time.sleep(5 * attempt)
                        continue
                raise exc
            result = holder.get("result")
            info = (result or {}).get("info") or {}
            error = info.get("error")
            if not error:
                return result
            data = error.get("data") or {}
            retryable = bool(
                error.get("isRetryable")
                or data.get("isRetryable")
                or error.get("statusCode") in (429, 500, 502, 503)
            )
            last_error = OpenCodeError(
                "ошибка провайдера: " + json.dumps(error, ensure_ascii=False)[:500]
            )
            if retryable and attempt < attempts:
                delay = (data.get("responseHeaders") or {}).get("retry-after")
                time.sleep(min(max(float(delay or 5), 5.0), 120.0))
                continue
            raise last_error
        raise last_error or OpenCodeError("неизвестная ошибка провайдера")

    def messages(self, session_id: str):
        return self.request(
            "GET", f"/session/{quote(session_id, safe='')}/message", timeout=60
        ) or []

    def abort(self, session_id: str):
        try:
            self.request("POST", f"/session/{quote(session_id, safe='')}/abort", {}, timeout=30)
        except OpenCodeError:
            pass


def extract_text(result) -> str:
    if not isinstance(result, dict):
        return ""
    parts = result.get("parts")
    if not parts and isinstance(result.get("info"), dict):
        parts = result.get("parts") or []
    chunks = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


# ------------------------------------------------------------------------- server

def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def load_server() -> dict | None:
    try:
        return json.loads(SERVER_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def config_mtime() -> float:
    files = [PROJECT / "opencode.json"]
    tools_dir = PROJECT / ".opencode" / "tools"
    if tools_dir.is_dir():
        files.extend(tools_dir.glob("*.ts"))
    prompts = PROJECT / "prompts"
    if prompts.is_dir():
        files.extend(prompts.glob("*.md"))
    stamps = [f.stat().st_mtime for f in files if f.exists()]
    return max(stamps) if stamps else 0.0


MODEL_CACHE: dict = {"mtime": None, "models": {}}


def parse_model_id(value: str | None) -> dict | None:
    provider, sep, model = (value or "").partition("/")
    if not sep or not provider or not model:
        return None
    return {"providerID": provider, "modelID": model}


def agent_config_models() -> dict[str, str]:
    """profile -> configured model string, re-read whenever opencode.json changes."""
    path = PROJECT / "opencode.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if MODEL_CACHE.get("mtime") != mtime:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        MODEL_CACHE["mtime"] = mtime
        MODEL_CACHE["models"] = {
            name: (cfg or {}).get("model")
            for name, cfg in (data.get("agent") or {}).items()
            if isinstance(cfg, dict)
        }
    return dict(MODEL_CACHE.get("models") or {})


def tools_mtime() -> float:
    """Newest mtime of custom tools/prompts; they require a server process restart."""
    files = []
    tools_dir = PROJECT / ".opencode" / "tools"
    if tools_dir.is_dir():
        files.extend(tools_dir.glob("*.ts"))
    prompts = PROJECT / "prompts"
    if prompts.is_dir():
        files.extend(prompts.glob("*.md"))
    stamps = [f.stat().st_mtime for f in files if f.exists()]
    return max(stamps) if stamps else 0.0


def agent_profile_model(profile: str) -> dict | None:
    return parse_model_id(agent_config_models().get(profile))


def pid_is_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def stop_pid(pid, force: bool = False) -> None:
    try:
        os.kill(int(pid), 9 if force else 15)
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        pass


def _spawn_server(port: int, password: str) -> subprocess.Popen:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = open(RUN_DIR / "server.log", "ab")
    env = os.environ.copy()
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["TEAM_DB"] = str(DB_PATH)
    return subprocess.Popen(
        ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _start_server(port: int, password: str, verbose: bool, base: dict | None = None) -> dict:
    try:
        proc = _spawn_server(port, password)
    except FileNotFoundError:
        raise SystemExit("opencode не найден в PATH") from None
    url = f"http://127.0.0.1:{port}"
    client = OpenCode(url, password)
    deadline = time.time() + 60
    while time.time() < deadline:
        if client.health():
            info = dict(base or {})
            info.update(
                {
                    "url": url,
                    "port": port,
                    "password": password,
                    "pid": proc.pid,
                    "project": str(PROJECT),
                    "started": time.time(),
                }
            )
            SERVER_FILE.write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
            if verbose:
                print(f"Сервер готов: {url}")
            return info
        if proc.poll() is not None:
            tail = ""
            try:
                tail = (RUN_DIR / "server.log").read_text("utf-8", "replace")[-2000:]
            except OSError:
                pass
            raise SystemExit(f"opencode serve завершился при старте:\n{tail}")
        time.sleep(0.5)
    raise SystemExit("opencode serve не ответил за 60 секунд; смотри run/server.log")


def _wait_port_free(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_is_free(port):
            return True
        time.sleep(0.3)
    return port_is_free(port)


def _stop_and_wait(pid, timeout: float = 15.0) -> None:
    if not pid or not pid_is_alive(pid):
        return
    stop_pid(pid)
    deadline = time.time() + timeout
    while pid_is_alive(pid) and time.time() < deadline:
        time.sleep(0.3)
    if pid_is_alive(pid):
        stop_pid(pid, force=True)
        time.sleep(0.5)


def ensure_server(port_start: int, verbose: bool = True) -> dict:
    saved = load_server()
    if saved:
        client = OpenCode(saved["url"], saved.get("password"))
        stale = float(saved.get("started") or 0) < config_mtime()
        if not stale and client.health():
            if verbose:
                print(f"Использую уже запущенный сервер {saved['url']}")
            return saved
        if saved.get("pid") and saved.get("port") and saved.get("password"):
            if verbose:
                reason = "конфигурация изменилась" if stale else "сервер не отвечает"
                print(f"{reason} — перезапускаю сервер {saved['url']}")
            _stop_and_wait(saved["pid"])
            if _wait_port_free(saved["port"]):
                return _start_server(saved["port"], saved["password"], verbose, base=saved)

    password = secrets.token_urlsafe(18)
    port = None
    for candidate in range(port_start, port_start + 50):
        if port_is_free(candidate):
            port = candidate
            break
    if port is None:
        raise SystemExit("не найден свободный порт для opencode serve")
    return _start_server(port, password, verbose)


def reload_config(timeout: float = 15.0, verbose: bool = True) -> tuple[bool, str]:
    """Hot-reload the opencode instance config without killing the server.

    POST /instance/dispose releases the instance; the next request rebuilds it from
    opencode.json. The server process, port, password and sessions survive.
    """
    server = load_server()
    if not server:
        try:
            server = ensure_server(4096, verbose=verbose)
        except SystemExit as exc:
            return False, str(exc)
    client = OpenCode(server["url"], server.get("password"), timeout=60)
    expected = {
        profile: model
        for profile, model in agent_config_models().items()
        if model
    }
    try:
        client.request("POST", "/instance/dispose", {}, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return False, f"не удалось перезагрузить конфиг: {exc}"
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            config = client.request("GET", "/config", timeout=30)
            agents = (config or {}).get("agent") or {}
            last = {
                profile: (agents.get(profile) or {}).get("model")
                for profile in expected
            }
            if all(last.get(profile) == model for profile, model in expected.items()):
                return True, "Конфиг перезагружен: модели применены к текущим сессиям."
        except Exception:  # noqa: BLE001 - instance is rebuilding
            pass
        time.sleep(0.3)
    if not expected:
        return True, "Конфиг перезагружен."
    return False, (
        "экземпляр перезагружен, но конфиг не совпал: "
        + json.dumps(last, ensure_ascii=False)
    )


def restart_server_keep_auth(verbose: bool = True) -> tuple[bool, str]:
    """Restart opencode serve keeping port and password so connected clients survive."""
    saved = load_server()
    if not saved or not saved.get("port") or not saved.get("password"):
        try:
            info = ensure_server(4096, verbose=verbose)
        except SystemExit as exc:
            return False, str(exc)
        return True, f"Сервер запущен: {info['url']}"
    _stop_and_wait(saved.get("pid"))
    if not _wait_port_free(saved["port"]):
        return False, f"порт {saved['port']} занят после остановки сервера"
    try:
        info = _start_server(saved["port"], saved["password"], verbose, base=saved)
    except SystemExit as exc:
        return False, str(exc)
    return True, f"Сервер перезапущен: {info['url']}, модели из конфига применены"


def ensure_plugin_deps() -> None:
    marker = PROJECT / ".opencode" / "node_modules" / "@opencode-ai" / "plugin"
    if marker.exists():
        return
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit(
            "не установлены зависимости плагина team и нет npm. "
            "Выполните: cd .opencode && npm install"
        )
    print("Устанавливаю зависимости .opencode (npm install)...")
    proc = subprocess.run(
        [npm, "install", "--no-audit", "--no-fund"],
        cwd=str(PROJECT / ".opencode"),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"npm install не удался:\n{proc.stdout}\n{proc.stderr}")


def write_attach_helpers(server: dict, sessions: dict[str, str]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    lines = ["#!/usr/bin/env bash", "set -e", 'ROLE="${1:-}"', 'case "$ROLE" in']
    for role in ROLES:
        lines.append(
            f'  {role}) exec opencode attach "{server["url"]}" '
            f'-s "{sessions[role]}" -p "{server["password"]}" --dir "{PROJECT}" ;;'
        )
    lines += [
        '  *) echo "usage: $0 <controller|researcher|developer|tester>"; exit 1 ;;',
        "esac",
    ]
    script = RUN_DIR / "attach.sh"
    script.write_text("\n".join(lines) + "\n", "utf-8")
    script.chmod(0o755)

    md = ["# Наблюдение за агентами", ""]
    md.append("```bash")
    for role in ROLES:
        md.append(f"# {role}")
        md.append(
            f'opencode attach "{server["url"]}" -s "{sessions[role]}" '
            f'-p "{server["password"]}" --dir "{PROJECT}"'
        )
        md.append("")
    md.append("# или")
    for role in ROLES:
        md.append(f"bash run/attach.sh {role}")
    md.append("```")
    (RUN_DIR / "attach.md").write_text("\n".join(md) + "\n", "utf-8")


# ------------------------------------------------------------------- orchestrator

class Orchestrator:
    def __init__(
        self,
        task: str,
        server: dict,
        sessions: dict[str, str],
        timeout: int,
        debug: bool = False,
        db_path: Path | None = None,
        run_dir: Path | None = None,
    ):
        self.task = task.strip()
        self.server = server
        self.sessions = sessions
        self.client = OpenCode(server["url"], server["password"], timeout=timeout)
        self.debug = debug
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.run_dir = Path(run_dir) if run_dir else RUN_DIR
        self.conn = team_db.connect(self.db_path)

    # -- logging ---------------------------------------------------------
    def log(self, message: str) -> None:
        line = f"[{now_str()}] {message}"
        print(line, flush=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with open(self.run_dir / "events.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def chat(self, role: str, kind: str, agent: str, text: str) -> None:
        chat_dir = self.run_dir / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        path = chat_dir / f"{role}.md"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"\n\n## [{now_str()}] {kind} · {agent}\n\n{text}\n")

    def set_phase(self, role: str, phase: str) -> None:
        team_db.set_phase(self.conn, role, phase)
        current = team_db.get_phase(self.conn, role)
        if current != phase:
            raise RuntimeError(f"не удалось переключить {role} в фазу {phase}")

    def last_event_id(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
        return int(row["m"])

    def send(self, role: str, phase: str, message: str, restart: bool = False) -> str:
        agent = f"{role}-{phase}"
        if restart:
            team_db.set_phase(self.conn, role, phase)
        else:
            team_db.ensure_phase(self.conn, role, phase)
        if team_db.get_phase(self.conn, role) != phase:
            raise RuntimeError(f"не удалось переключить {role} в фазу {phase}")
        prompt_time = time.strftime("%H:%M:%S")
        self.log(f"→ {agent}: {short(message.splitlines()[0] if message.splitlines() else message)}")
        self.chat(role, "ПРОМПТ", agent, message)
        before = self.last_event_id()
        model = agent_profile_model(agent)
        for attempt in (1, 2):
            try:
                result = self.client.prompt(self.sessions[role], agent, message, model=model)
                break
            except OpenCodeError as exc:
                self.log(f"✗ {agent}: {exc}")
                if (self.run_dir / "cancel").exists():
                    raise RunCancelled("прогон отменён оператором") from None
                if self.last_event_id() > before:
                    raise
                if attempt == 2:
                    raise
                self.log(f"↻ {agent}: повтор хода после сбоя ({prompt_time})")
                time.sleep(3)
        text = extract_text(result)
        self.chat(role, "ОТВЕТ", agent, text or "(пустой текстовый ответ; см. инструменты в сессии)")
        self.log(f"← {agent}: {short(text) if text else '(ответ без текста)'}")
        return text

    # -- task-building messages -----------------------------------------
    @property
    def directive(self) -> str:
        return team_db.get_meta(self.conn, "controller_plan", "") or ""

    def task_header(self) -> str:
        return f"ЗАДАНИЕ ПОЛЬЗОВАТЕЛЯ:\n{self.task}"

    def tasks_summary(self) -> str:
        rows = team_db.list_tasks(self.conn)
        if not rows:
            return "Задач пока нет."
        return "\n".join(
            f"- {r['id']} ({r['worker']}): {r['status']}; цель: {short(r['goal'], 120)}"
            for r in rows
        )

    # -- lifecycle -------------------------------------------------------
    def run(self) -> None:
        team_db.set_meta(self.conn, "run_status", "running")
        team_db.set_meta(self.conn, "task", self.task)
        self.log(f"СТАРТ: {self.task}")
        message = (
            f"{self.task_header()}\n\n"
            "Ты — контролёр, фаза PLAN. Сформулируй директиву для исследователя: цель, "
            "критерии приёмки, ограничения, риски. Когда директива готова, вызови "
            'team submit с phase="plan" и полным текстом директивы.'
        )
        self.send("controller", "plan", message)

        while team_db.get_meta(self.conn, "run_status", "running") == "running":
            if (self.run_dir / "cancel").exists():
                self.log("получена команда отмены — прерываю сессии")
                team_db.set_meta(self.conn, "run_status", "cancelled")
                for session_id in self.sessions.values():
                    self.client.abort(session_id)
                break
            event = team_db.next_event(self.conn)
            if event is None:
                time.sleep(0.4)
                continue
            self.handle_event(event)
            team_db.consume_event(self.conn, event["id"])

        if team_db.get_meta(self.conn, "run_status") == "cancelled":
            self.log("ПРОГОН ОТМЕНЁН. Сессии оставлены открытыми для просмотра.")
        else:
            self.log("ГОТОВО. Сессии оставлены открытыми для просмотра.")

    # -- human channel ---------------------------------------------------
    def announce_human_question(self, qid: int, header: str, text: str) -> None:
        print("\n" + "┌" + "─" * 78, flush=True)
        print(f"│ ВОПРОС ЧЕЛОВЕКУ  [q{qid}]  {header}", flush=True)
        print("├" + "─" * 78, flush=True)
        for line in text.splitlines() or [""]:
            print("│ " + line, flush=True)
        print("├" + "─" * 78, flush=True)
        print(
            f'│ Ответить: python3 team.py answer q{qid} "<текст>"   '
            "(approve/revise — для плана и финального результата)",
            flush=True,
        )
        print("└" + "─" * 78 + "\n", flush=True)

    # -- event dispatch --------------------------------------------------
    def handle_event(self, event) -> None:
        role = event["role"]
        action = event["action"]
        payload = json.loads(event["payload"])
        self.current_event_id = int(event["id"])
        self.log(f"событие #{event['id']}: {role} {action}")
        handler = getattr(self, f"on_{action}", None)
        if handler is None:
            self.log(f"неизвестное событие {action}, пропускаю")
            return
        handler(role, payload)

    def on_sources(self, role: str, payload: dict) -> None:
        self.log(f"источник от исследователя: {short(payload.get('url', ''), 120)}")

    def on_submit(self, role: str, payload: dict) -> None:
        phase = payload.get("phase")
        content = payload.get("content", "")
        task_id = payload.get("task_id")
        if team_db.is_routed(self.conn, role, phase):
            self.log(
                f"повторный submit {role}/{phase} проигнорирован: "
                "результат этой фазы уже маршрутизирован"
            )
            return
        team_db.mark_routed(self.conn, role, phase)

        if role == "controller" and phase == "plan":
            team_db.set_meta(self.conn, "controller_plan", content)
            qid = team_db.new_question(
                self.conn,
                "controller",
                "human",
                "approve_controller_plan",
                f"Контролёр предлагает директиву:\n\n{content}\n\n"
                "Принять директиву? Напишите approve или замечания для доработки.",
            )
            self.announce_human_question(qid, "контролёр · план", "Согласование директивы контролёра")
            return

        if role == "controller" and phase == "build":
            qid = team_db.new_question(
                self.conn,
                "controller",
                "human",
                "accept_final",
                f"Финальное заключение контролёра:\n\n{content}\n\n"
                "Принять результат? Напишите approve или замечания.",
            )
            self.announce_human_question(qid, "контролёр · финал", "Приёмка финального результата")
            return

        if role == "researcher" and phase == "plan":
            message = (
                f"{self.task_header()}\n\n"
                f"ДИРЕКТИВА КОНТРОЛЁРА:\n{self.directive}\n\n"
                f"ИССЛЕДОВАТЕЛЬ ПРИСЛАЛ ПЛАН НА СВЕРКУ:\n{content}\n\n"
                "Ты — контролёр, фаза BUILD. Проверь соответствие заданию пользователя и директиве. "
                'Вызови team review с phase="plan", verdict="approve"|"revise" и comments.'
            )
            self.send("controller", "build", message)
            return

        if role == "researcher" and phase == "build":
            message = (
                f"{self.task_header()}\n\n"
                f"ДИРЕКТИВА КОНТРОЛЁРА:\n{self.directive}\n\n"
                f"СОСТОЯНИЕ ЗАДАЧ:\n{self.tasks_summary()}\n\n"
                f"ИССЛЕДОВАТЕЛЬ ПРИСЛАЛ ФИНАЛЬНЫЙ ОТЧЁТ:\n{content}\n\n"
                "Ты — контролёр, фаза BUILD. Проверь соответствие заданию пользователя. "
                'Вызови team review с phase="build", verdict="approve"|"revise" и comments. '
                "При несоответствии заданию можно задать вопрос человеку через team ask."
            )
            self.send("controller", "build", message)
            return

        if role in ("developer", "tester"):
            if not task_id:
                self.log("submit без task_id от исполнителя — пропускаю")
                return
            task = team_db.get_task(self.conn, task_id)
            worker_ru = ROLE_RU.get(role, role)
            kind = "ПЛАН" if phase == "plan" else "ОТЧЁТ"
            message = (
                f"{worker_ru.upper()} ПРИСЛАЛ {kind} по задаче {task_id}.\n"
                f"Цель: {task['goal']}\n\n"
                f"МАТЕРИАЛ:\n{content}\n\n"
                "Ты — исследователь, фаза BUILD. Проверь результат и вызови team review с "
                f'task_id="{task_id}", phase="{phase}", verdict="approve"|"revise", comments.'
            )
            self.send("researcher", "build", message)
            return

        self.log(f"submit от {role}/{phase} не обработан")

    def on_assign(self, role: str, payload: dict) -> None:
        task_id = payload["task_id"]
        worker = payload["worker"]
        goal = payload["goal"]
        context = payload.get("context", "")
        team_db.set_task_status(self.conn, task_id, "plan_pending")
        message = (
            f"Ты — {ROLE_RU[worker].lower()}, фаза PLAN.\n\n"
            f"ЗАДАЧА {task_id} ОТ ИССЛЕДОВАТЕЛЯ:\n{goal}\n\n"
            f"КОНТЕКСТ / АРХИТЕКТУРА:\n{context or '(без дополнительного контекста)'}\n\n"
            f"{self.task_header()}\n\n"
            "Работай в workspace/. Сдай план через team submit: "
            f'phase="plan", task_id="{task_id}".'
        )
        self.send(worker, "plan", message)

    def on_review(self, role: str, payload: dict) -> None:
        phase = payload["phase"]
        verdict = payload["verdict"]
        comments = payload.get("comments", "")
        task_id = payload.get("task_id")

        if role == "controller":
            if phase == "plan" and verdict == "approve":
                self.log("контролёр принял план исследователя → BUILD")
                message = (
                    f"План исследователя утверждён контролёром.\n\n"
                    f"{self.task_header()}\n\n"
                    f"ДИРЕКТИВА КОНТРОЛЁРА:\n{self.directive}\n\n"
                    "Ты — исследователь, фаза BUILD. Создай задачи через team assign "
                    "(разработчику — реализация, тестировщику — проверка по существу), затем "
                    "координируй их и сдай финальный отчёт team submit phase=\"build\"."
                )
                self.send("researcher", "build", message)
                return
            if phase == "plan" and verdict == "revise":
                message = (
                    "Контролёр отклонил твой план и просит доработать.\n\n"
                    f"ЗАМЕЧАНИЯ:\n{comments}\n\n"
                    f"{self.task_header()}\n\n"
                    f"ДИРЕКТИВА КОНТРОЛЁРА:\n{self.directive}\n\n"
                    "Пересмотри план и снова сдай его через team submit phase=\"plan\"."
                )
                self.send("researcher", "plan", message, restart=True)
                return
            if phase == "build" and verdict == "approve":
                if team_db.is_routed(self.conn, "controller", "build") or team_db.has_pending_submit(
                    self.conn, "controller", "build", getattr(self, "current_event_id", 0)
                ):
                    self.log(
                        "контролёр уже сдал финальное заключение в этом же ходе — "
                        "не дублирую приглашение"
                    )
                    return
                message = (
                    "Ты принял финальный отчёт исследователя.\n\n"
                    f"{self.task_header()}\n\n"
                    "Сформируй финальное заключение для человека: что сделано, соответствие "
                    "заданию, риски, вердикт. Вызови team submit с phase=\"build\"."
                )
                self.send("controller", "build", message)
                return
            if phase == "build" and verdict == "revise":
                message = (
                    "Контролёр не принял твой финальный отчёт.\n\n"
                    f"ЗАМЕЧАНИЯ:\n{comments}\n\n"
                    f"{self.task_header()}\n\n"
                    "Доработай результат (при необходимости создай задачи через team assign) "
                    "и снова сдай отчёт через team submit phase=\"build\"."
                )
                self.send("researcher", "build", message, restart=True)
                return

        if role == "researcher":
            task = team_db.get_task(self.conn, task_id) if task_id else None
            if not task:
                self.log("review от исследователя без корректной задачи — пропускаю")
                return
            worker = task["worker"]
            worker_ru = ROLE_RU[worker].lower()
            if verdict == "approve" and phase == "plan":
                team_db.set_task_status(self.conn, task_id, "build_pending")
                message = (
                    f"Твой план по задаче {task_id} утверждён исследователем.\n"
                    f"ЗАМЕЧАНИЯ: {comments or 'нет'}\n\n"
                    "Приступай к BUILD: реализуй/проверь в workspace/, собери фактические "
                    f'результаты и сдай их через team submit: phase="build", task_id="{task_id}".'
                )
                self.send(worker, "build", message)
                return
            if verdict == "approve" and phase == "build":
                team_db.set_task_status(self.conn, task_id, "done")
                if team_db.is_routed(self.conn, "researcher", "build") or team_db.has_pending_submit(
                    self.conn, "researcher", "build", getattr(self, "current_event_id", 0)
                ):
                    self.log(
                        "исследователь уже сдал финальный отчёт в этом же ходе — "
                        "не дублирую приглашение"
                    )
                    return
                message = (
                    f"Отчёт по задаче {task_id} ({worker_ru}) принят, задача закрыта.\n\n"
                    "Проверь team status: остались ли активные задачи.\n"
                    "Если все задачи закрыты — проанализируй отчёты и сдай финальный отчёт "
                    "team submit phase=\"build\". Если нужны ещё задачи — создай их через team assign."
                )
                self.send("researcher", "build", message)
                return
            if verdict == "revise":
                attempts = team_db.bump_attempts(self.conn, task_id)
                team_db.set_task_status(self.conn, task_id, f"{phase}_pending")
                if attempts > 5:
                    self.log(f"ВНИМАНИЕ: задача {task_id} дорабатывается {attempts}-й раз")
                todo = "план" if phase == "plan" else "отчёт"
                message = (
                    f"Исследователь отклонил твой {todo} по задаче {task_id}.\n\n"
                    f"ЗАМЕЧАНИЯ:\n{comments}\n\n"
                    f"Пересмотри {todo} и снова сдай его через team submit: "
                    f'phase="{phase}", task_id="{task_id}".'
                )
                self.send(worker, phase, message, restart=True)
                return

        self.log(f"review от {role} не обработан: {payload}")

    def on_inject(self, role: str, payload: dict) -> None:
        text = (payload.get("text") or "").strip()
        target = payload.get("target") or "controller"
        if not text:
            return
        if target != "controller":
            self.log(f"inject для {target} пока не поддерживается")
            return
        phase = team_db.get_phase(self.conn, "controller") or "plan"
        message = (
            "СООБЩЕНИЕ ОТ ЧЕЛОВЕКА (передано секретарём):\n"
            f"{text}\n\n"
            "Учти это в текущей работе. Если нужно изменить задачи или план — действуй "
            "через свои обычные инструменты; при неоднозначности задай вопрос через team ask."
        )
        self.log("сообщение человека доставляется контролёру")
        self.send("controller", phase, message)

    def on_ask(self, role: str, payload: dict) -> None:
        qid = payload["question_id"]
        question = team_db.get_question(self.conn, qid)
        if question is None:
            self.log(f"ask ссылается на неизвестный вопрос q{qid}")
            return
        to_role = question["to_role"]
        if to_role == "human":
            self.announce_human_question(qid, f"{ROLE_RU[role]} → человек", question["text"])
            return
        phase = team_db.get_phase(self.conn, to_role) or "build"
        header = {
            "researcher": "РАЗРАБОТЧИК ИЛИ ТЕСТИРОВЩИК",
            "controller": "ИССЛЕДОВАТЕЛЬ",
        }.get(role, role.upper())
        message = (
            f"ВОПРОС ОТ {header} (q{qid}):\n{question['text']}\n\n"
            f"{self.task_header()}\n\n"
            f"Ты — {ROLE_RU[to_role].lower()}. Ответь через team answer: "
            f'question_id="q{qid}", answer="..."'
        )
        self.send(to_role, phase, message)

    def on_answer(self, role: str, payload: dict) -> None:
        qid = payload["question_id"]
        text = payload.get("text", "")
        question = team_db.get_question(self.conn, qid)
        if not question:
            self.log(f"ответ на неизвестный вопрос q{qid}")
            return

        if question["kind"] == "approve_controller_plan":
            if is_approval(text):
                self.log("человек утвердил директиву контролёра → исследователь PLAN")
                team_db.set_phase(self.conn, "controller", "build")
                message = (
                    f"Директива контролёра утверждена человеком.\n\n"
                    f"{self.task_header()}\n\n"
                    f"ДИРЕКТИВА КОНТРОЛЁРА:\n{self.directive}\n\n"
                    "Ты — исследователь, фаза PLAN. Найди источники, декомпозируй задачу, "
                    "спроектируй архитектуру и сдай план через team submit phase=\"plan\"."
                )
                self.send("researcher", "plan", message)
            else:
                self.log("человек отправил директиву контролёра на доработку")
                message = (
                    "Человек не утвердил директиву и просит доработать.\n\n"
                    f"ЗАМЕЧАНИЯ ЧЕЛОВЕКА:\n{text}\n\n"
                    f"{self.task_header()}\n\n"
                    "Пересмотри директиву и снова сдай её через team submit phase=\"plan\"."
                )
                self.send("controller", "plan", message, restart=True)
            return

        if question["kind"] == "accept_final":
            if is_approval(text):
                team_db.set_meta(self.conn, "run_status", "done")
                self.log("человек принял финальный результат — прогон завершён")
            else:
                self.log("человек не принял финальное заключение → контролёр дорабатывает")
                message = (
                    "Человек не принял финальный результат.\n\n"
                    f"ЗАМЕЧАНИЯ ЧЕЛОВЕКА:\n{text}\n\n"
                    f"{self.task_header()}\n\n"
                    "Пересмотри заключение и снова сдай его через team submit phase=\"build\"."
                )
                self.send("controller", "build", message, restart=True)
            return

        asker = question["from_role"]
        phase = question["phase"] or team_db.get_phase(self.conn, asker) or "build"
        answer_from = "ЧЕЛОВЕКА" if role == "human" else ROLE_RU.get(role, role).upper()
        message = (
            f"ОТВЕТ ОТ {answer_from} на твой вопрос q{qid}:\n{text}\n\n"
            f"Продолжай работу в фазе {phase}. Если результат фазы готов — team submit."
        )
        self.send(asker, phase, message)


# ------------------------------------------------------------------------ helpers

def reset_run_state() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            (RUN_DIR / f"state.db{suffix}").unlink()
        except FileNotFoundError:
            pass
    try:
        LOG_FILE.unlink()
    except FileNotFoundError:
        pass
    if CHAT_DIR.exists():
        shutil.rmtree(CHAT_DIR)
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    team_db.init(DB_PATH)


def create_sessions(client: OpenCode, run_tag: str) -> dict[str, str]:
    sessions = {}
    for role in ROLES:
        title = f"team/{run_tag}/{role}"
        sessions[role] = client.create_session(title)
    return sessions


def start_stdin_reader(orch: Orchestrator) -> None:
    if not sys.stdin.isatty():
        return

    def loop() -> None:
        while True:
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                return
            line = line.strip()
            if not line:
                continue
            conn = team_db.connect(DB_PATH)
            try:
                open_qs = team_db.open_questions(conn, "human")
                if not open_qs:
                    print("Нет открытых вопросов человеку. Используйте team.py status / answer.")
                    continue
                q = open_qs[0]
                team_db.answer_question(conn, q["id"], line)
                team_db.add_event(
                    conn, "human", "answer", {"question_id": q["id"], "text": line, "from_role": "human"}
                )
                print(f"Ответ на q{q['id']} принят.")
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------------------- CLI

def cmd_run(args) -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), "utf-8")
    try:
        try:
            CANCEL_FILE.unlink()
        except FileNotFoundError:
            pass
        ensure_plugin_deps()
        if not (PROJECT / "opencode.json").exists():
            raise SystemExit("нет opencode.json рядом с team.py")
        server = ensure_server(args.port)
        run_tag = time.strftime("%Y%m%d-%H%M%S")
        reset_run_state()
        conn = team_db.connect(DB_PATH)
        try:
            team_db.set_meta(conn, "run_tag", run_tag)
            team_db.set_meta(conn, "task", args.task.strip())
        finally:
            conn.close()

        client = OpenCode(server["url"], server["password"], timeout=args.timeout)
        sessions = create_sessions(client, run_tag)
        conn = team_db.connect(DB_PATH)
        try:
            for role, session_id in sessions.items():
                team_db.set_session(conn, role, session_id)
        finally:
            conn.close()

        write_attach_helpers(server, sessions)
        print("\nСессии созданы. Наблюдение в отдельных терминалах:")
        for role in ROLES:
            print(
                f"  {role:<11} opencode attach {server['url']} -s {sessions[role]} "
                f"-p {server['password']} --dir {PROJECT}"
            )
        print("\nКратко: bash run/attach.sh <controller|researcher|developer|tester>")
        print("События: run/events.log · чаты: run/chat/<role>.md · состояние: team.py status\n")

        orch = Orchestrator(args.task, server, sessions, args.timeout, debug=args.debug)
        start_stdin_reader(orch)
        try:
            orch.run()
        except KeyboardInterrupt:
            print("\nОстановлено пользователем. Сервер оставлен запущенным (team.py stop).")
            return 130
        except RunCancelled:
            print("\nПрогон отменён оператором.")
            return 130
        except Exception:
            print("\nОШИБКА оркестратора:", file=sys.stderr)
            traceback.print_exc()
            print("Сессии и журнал сохранены в run/.", file=sys.stderr)
            return 1
        return 0
    finally:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass


def cmd_status(args) -> int:
    path = Path(args.db) if args.db else DB_PATH
    if not path.exists():
        print("Состояния пока нет: запустите team.py run")
        return 1
    conn = team_db.connect(path)
    try:
        print(team_db.summary(conn))
        qs = team_db.open_questions(conn)
        if qs:
            print("\nВопросы человеку:")
            for q in qs:
                print(f"- q{q['id']}: {q['text']}")
        print(f"\nrun_status={team_db.get_meta(conn, 'run_status', '?')}")
        server = load_server()
        if server:
            print(f"server={server['url']} (pid {server.get('pid')})")
    finally:
        conn.close()
    return 0


def format_event(event) -> str:
    payload = json.loads(event["payload"])
    detail = {
        "submit": lambda p: f'phase={p.get("phase")} task={p.get("task_id")}',
        "review": lambda p: f'phase={p.get("phase")} task={p.get("task_id")} verdict={p.get("verdict")}',
        "assign": lambda p: f'task={p.get("task_id")} worker={p.get("worker")}',
        "ask": lambda p: f'q{p.get("question_id")} → {p.get("to_role")}',
        "answer": lambda p: f'q{p.get("question_id")} от {p.get("from_role")}',
        "sources": lambda p: p.get("url", ""),
    }.get(event["action"], lambda p: "")
    return (
        f'#{event["id"]:<4} {time.strftime("%H:%M:%S", time.localtime(event["ts"]))} '
        f'{event["role"]:<10} {event["action"]:<8} {detail(payload)}'
    )


def cmd_events(args) -> int:
    path = Path(args.db) if args.db else DB_PATH
    conn = team_db.connect(path)
    try:
        for event in team_db.recent_events(conn, args.n):
            print(format_event(event))
    finally:
        conn.close()
    return 0


def cmd_watch(args) -> int:
    path = Path(args.db) if args.db else DB_PATH
    if not path.exists():
        print("Состояния пока нет: запустите team.py run", file=sys.stderr)
        return 1
    print("Ctrl+C — выход. Живая лента событий; вопросы человеку печатаются один раз.")
    conn = team_db.connect(path)
    last = 0
    seen_questions: set[int] = set()
    try:
        while True:
            for event in conn.execute(
                "SELECT * FROM events WHERE id>? ORDER BY id", (last,)
            ).fetchall():
                print(format_event(event), flush=True)
                last = event["id"]
            for question in team_db.open_questions(conn, "human"):
                if question["id"] in seen_questions:
                    continue
                seen_questions.add(question["id"])
                print(
                    f'\n>>> ВОПРОС ЧЕЛОВЕКУ q{question["id"]} '
                    f'({question["from_role"]} → human, {question["kind"]})\n'
                    f'{question["text"][:600]}\n'
                    f'Ответить: python3 team.py answer q{question["id"]} "<текст>"\n',
                    flush=True,
                )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nвыход из watch")
        return 0
    finally:
        conn.close()


def cmd_answer(args) -> int:
    path = Path(args.db) if args.db else DB_PATH
    if not path.exists():
        print("Состояния пока нет: запустите team.py run", file=sys.stderr)
        return 1
    conn = team_db.connect(path)
    try:
        question = team_db.get_question(conn, args.qid)
        if not question:
            print(f"вопрос {args.qid} не найден", file=sys.stderr)
            return 1
        if question["status"] != "open":
            print(f"вопрос q{question['id']} уже закрыт", file=sys.stderr)
            return 1
        text = " ".join(args.text).strip()
        try:
            qid = team_db.answer_human(conn, question["id"], text)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Ответ на q{qid} принят.")
    finally:
        conn.close()
    return 0


def cmd_cancel(args) -> int:
    import run_control

    if not run_control.is_active():
        print("Активного прогона нет.")
        return 0
    run_control.cancel_run()
    print("Отмена запрошена; оркестратор остановится после текущего шага.")
    return 0


def cmd_stop(args) -> int:
    server = load_server()
    if not server:
        print("Нет сохранённого сервера.")
        return 0
    pid = server.get("pid")
    if not pid:
        print("В server.json нет pid.")
        return 1
    try:
        os.kill(int(pid), 15)
        print(f"Отправлен SIGTERM серверу pid={pid}.")
    except ProcessLookupError:
        print("Сервер уже остановлен.")
    except PermissionError:
        print("Нет прав остановить процесс.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="team.py", description="AutomateML multi-agent orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="start a run")
    run_p.add_argument("task")
    run_p.add_argument("--port", type=int, default=4096)
    run_p.add_argument("--timeout", type=int, default=1800, help="turn timeout in seconds")
    run_p.add_argument("--debug", action="store_true")
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status", help="state overview")
    status_p.add_argument("--db")
    status_p.set_defaults(func=cmd_status)

    events_p = sub.add_parser("events", help="recent events")
    events_p.add_argument("-n", type=int, default=30)
    events_p.add_argument("--db")
    events_p.set_defaults(func=cmd_events)

    watch_p = sub.add_parser("watch", help="live event feed (follow progress)")
    watch_p.add_argument("--db")
    watch_p.set_defaults(func=cmd_watch)

    answer_p = sub.add_parser("answer", help="answer a human question")
    answer_p.add_argument("qid")
    answer_p.add_argument("text", nargs="+")
    answer_p.add_argument("--db")
    answer_p.set_defaults(func=cmd_answer)

    cancel_p = sub.add_parser("cancel", help="cancel the active run")
    cancel_p.set_defaults(func=cmd_cancel)

    stop_p = sub.add_parser("stop", help="stop the opencode server started by a run")
    stop_p.set_defaults(func=cmd_stop)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))