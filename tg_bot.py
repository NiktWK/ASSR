#!/usr/bin/env python3
"""Telegram bot for the AutomateML team (aiogram 3).

Direct commands manage the run; plain text talks to the secretary agent.
Agent conversations are never pushed to Telegram: progress is metadata only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import model_admin
import run_control
import team
import team_db

PROJECT = Path(__file__).resolve().parent
RUN_DIR = PROJECT / "run"
DB_PATH = RUN_DIR / "state.db"
STATE_FILE = RUN_DIR / "bot_state.json"
SECRETARY_FILE = RUN_DIR / "secretary.json"
CONFIG_FILE = RUN_DIR / "telegram.json"
LOG_FILE = RUN_DIR / "bot.log"

MAX_LEN = 4000
ROLE_RU = {
    "controller": "контролёр",
    "researcher": "исследователь",
    "developer": "разработчик",
    "tester": "тестировщик",
}
KIND_RU = {
    "approve_controller_plan": "согласование плана контролёра",
    "accept_final": "приёмка финального результата",
    "agent": "вопрос агента",
}
ROLE_PROFILES = model_admin.ROLE_PROFILES
SECRETARY_PROFILE = model_admin.SECRETARY_PROFILE
ALL_MODEL_TARGETS = model_admin.ALL_MODEL_TARGETS
SHORT_ROLES = model_admin.SHORT_ROLES
PENDING_FILE = model_admin.PENDING_FILE
APPLY = {"dirty": False, "task": None, "restarting": False}
expand_targets = model_admin.expand_targets
write_model_config = model_admin.write_config
live_model_check = model_admin.live_check
mark_pending = model_admin.mark_pending
clear_pending = model_admin.clear_pending

router = Router()
secretary_lock = asyncio.Lock()
question_messages: dict[int, int] = {}
await_reply: dict[int, int] = {}


# --------------------------------------------------------------------- config/state

def load_config() -> tuple[str | None, int | None]:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_ALLOWED_CHAT_ID") or "").strip()
    if (not token or not chat) and CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        token = token or str(data.get("token") or "").strip()
        chat = chat or str(data.get("chat_id") or "").strip()
    try:
        chat_id = int(chat) if chat else None
    except ValueError:
        chat_id = None
    return token or None, chat_id


def load_state() -> dict:
    state = {
        "initialized": False,
        "run_tag": "",
        "last_event_id": 0,
        "sent_questions": [],
        "run_status": "",
    }
    try:
        data = json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return state
    if isinstance(data, dict):
        state.update(data)
    return state


def save_state(state: dict) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def parse_model_id(value: str) -> dict | None:
    return team.parse_model_id(value)


def secretary_model() -> dict | None:
    """Secretary model for a per-message override (server config cache independent)."""
    return team.agent_profile_model(SECRETARY_PROFILE)


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    text = (text or "").strip() or "(пусто)"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------- db helpers

def db_snapshot(last_event_id: int) -> dict:
    if not DB_PATH.exists():
        return {"no_db": True, "active": run_control.is_active()}
    conn = team_db.connect(DB_PATH)
    try:
        tag = team_db.get_meta(conn, "run_tag", "") or ""
        status = team_db.get_meta(conn, "run_status", "?") or "?"
        task = team_db.get_meta(conn, "task", "") or ""
        events = conn.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200", (last_event_id,)
        ).fetchall()
        questions = team_db.open_questions(conn, "human")
        return {
            "no_db": False,
            "run_tag": tag,
            "run_status": status,
            "task": task,
            "events": events,
            "questions": questions,
            "max_event": conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM events"
            ).fetchone()["m"],
            "active": run_control.is_active(),
        }
    finally:
        conn.close()


def status_text() -> str:
    if not DB_PATH.exists():
        return (
            "Прогона ещё нет.\n"
            "Запустить: /newtask <цель>\n"
            "Или спросите секретаря обычным текстом."
        )
    try:
        conn = team_db.connect(DB_PATH)
    except sqlite3.Error:
        return "База состояния сейчас обновляется. Повторите через секунду."
    try:
        status = team_db.get_meta(conn, "run_status", "?") or "?"
        tag = team_db.get_meta(conn, "run_tag", "") or "-"
        task = team_db.get_meta(conn, "task", "") or "-"
        phases = conn.execute("SELECT role, phase, submitted FROM sessions").fetchall()
        tasks = team_db.list_tasks(conn)
        questions = team_db.open_questions(conn, "human")
    except sqlite3.Error:
        return "База состояния сейчас обновляется. Повторите через секунду."
    finally:
        conn.close()
    lines = [
        f"run_status: {status}  (run_tag {tag})",
        f"оркестратор: {'активен' if run_control.is_active() else 'не активен'}",
        f"задача: {task[:300]}",
        "",
        "Роли:",
    ]
    for row in phases:
        lines.append(
            f"- {ROLE_RU.get(row['role'], row['role'])}: {row['phase'] or '-'}"
            f"{', сдано' if row['submitted'] else ''}"
        )
    if tasks:
        lines.append("Задачи:")
        for t in tasks:
            lines.append(f"- {t['id']} ({t['worker']}): {t['status']}")
    lines.append(f"Открытых вопросов: {len(questions)}")
    return "\n".join(lines)


def tasks_text() -> str:
    if not DB_PATH.exists():
        return "Задач нет."
    try:
        conn = team_db.connect(DB_PATH)
        try:
            rows = team_db.list_tasks(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return "База состояния сейчас обновляется. Повторите через секунду."
    if not rows:
        return "Задач нет."
    return "\n".join(
        f"- {r['id']} ({r['worker']}): {r['status']}\n  {r['goal'][:300]}" for r in rows
    )


def events_text(limit: int) -> str:
    if not DB_PATH.exists():
        return "Событий нет."
    try:
        conn = team_db.connect(DB_PATH)
        try:
            rows = team_db.recent_events(conn, limit)
        finally:
            conn.close()
    except sqlite3.Error:
        return "База состояния сейчас обновляется. Повторите через секунду."
    if not rows:
        return "Событий нет."
    return "\n".join(team_db.format_event_meta(r) for r in rows)


def open_human_questions():
    """None means the DB is not readable right now."""
    if not DB_PATH.exists():
        return []
    try:
        conn = team_db.connect(DB_PATH)
        try:
            return team_db.open_questions(conn, "human")
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def answer_human(qid, text: str) -> tuple[bool, str]:
    if not DB_PATH.exists():
        return False, "Нет активного прогона."
    try:
        conn = team_db.connect(DB_PATH)
    except sqlite3.Error:
        return False, "База состояния сейчас обновляется. Повторите через секунду."
    try:
        try:
            answered = team_db.answer_human(conn, qid, text)
        except ValueError as exc:
            return False, str(exc)
        return True, f"Ответ на q{answered} принят."
    except sqlite3.Error:
        return False, "База состояния сейчас обновляется. Повторите через секунду."
    finally:
        conn.close()


def event_line(event) -> str | None:
    try:
        payload = json.loads(event["payload"])
    except (KeyError, json.JSONDecodeError):
        return None
    role = ROLE_RU.get(event["role"], event["role"])
    action = event["action"]
    if action == "assign":
        return f"🔧 Задача {payload.get('task_id')} → {ROLE_RU.get(payload.get('worker'), payload.get('worker'))}"
    if action == "submit":
        what = "план" if payload.get("phase") == "plan" else "отчёт"
        task = f" ({payload['task_id']})" if payload.get("task_id") else ""
        return f"📤 {role}: {what}{task} сдан на сверку"
    if action == "review":
        verdict = "✅ принято" if payload.get("verdict") == "approve" else "✏️ на доработку"
        task = f" ({payload['task_id']})" if payload.get("task_id") else ""
        return f"🧐 {role}: {verdict} — {payload.get('phase')}{task}"
    if action == "ask":
        return f"❓ Вопрос q{payload.get('question_id')} отправлен {payload.get('to_role')}"
    if action == "answer":
        return f"💬 Получен ответ на q{payload.get('question_id')}"
    if action == "inject":
        return "📨 Указание оператора передано контролёру"
    return None


# ------------------------------------------------------------------ secretary bridge

def get_client(ensure: bool = True):
    server = team.load_server()
    if server:
        client = team.OpenCode(server["url"], server.get("password"), timeout=1800)
        if client.health():
            return client
    if not ensure:
        raise RuntimeError("opencode server недоступен")
    server = team.ensure_server(4096)
    return team.OpenCode(server["url"], server.get("password"), timeout=1800)


def _save_secretary(client, session_id: str) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    attach = (
        f"opencode attach {client.base_url} -s {session_id} "
        f"-p {client.password} --dir {PROJECT}"
    )
    SECRETARY_FILE.write_text(
        json.dumps(
            {"session_id": session_id, "created": time.time(), "attach": attach},
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )


def ensure_secretary_session(client) -> str:
    session_id = None
    try:
        session_id = json.loads(SECRETARY_FILE.read_text("utf-8")).get("session_id")
    except (OSError, json.JSONDecodeError):
        session_id = None
    if session_id:
        try:
            client.messages(session_id)
            _save_secretary(client, session_id)
            return session_id
        except team.OpenCodeError:
            session_id = None
    session_id = client.create_session("team/secretary")
    _save_secretary(client, session_id)
    return session_id


def prepare_secretary() -> str:
    client = get_client(ensure=True)
    session_id = ensure_secretary_session(client)
    logging.info("secretary session: %s", session_id)
    print(
        f"Секретарь: opencode attach {client.base_url} -s {session_id} "
        f"-p {client.password} --dir {PROJECT}"
    )
    return session_id


async def ask_secretary(message: Message, text: str) -> None:
    if APPLY["restarting"]:
        await message.answer("Сейчас применяю модели (перезапуск сервера). Повторите через несколько секунд.")
        return
    async with secretary_lock:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            client = await asyncio.to_thread(get_client)
            session_id = await asyncio.to_thread(ensure_secretary_session, client)
            result = await asyncio.to_thread(
                client.prompt,
                session_id,
                "secretary",
                text,
                2,
                300,
                model=secretary_model(),
            )
            reply = team.extract_text(result) or "(секретарь не дал текстового ответа)"
        except Exception as exc:  # noqa: BLE001 - report to the operator
            logging.exception("secretary failed")
            reply = f"Ошибка секретаря: {exc}"
    for chunk in split_text(reply):
        await message.answer(chunk)


# ------------------------------------------------------------------ model management

def _session_ids() -> dict[str, str]:
    ids: dict[str, str] = {}
    if DB_PATH.exists():
        try:
            conn = team_db.connect(DB_PATH)
            try:
                ids = {
                    row["role"]: row["session_id"]
                    for row in conn.execute("SELECT role, session_id FROM sessions")
                }
            finally:
                conn.close()
        except sqlite3.Error:
            ids = {}
    try:
        ids[SECRETARY_PROFILE] = json.loads(SECRETARY_FILE.read_text("utf-8"))["session_id"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return ids


def last_used_models() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        client = get_client(ensure=False)
    except Exception:  # noqa: BLE001
        return result
    for role, session_id in _session_ids().items():
        try:
            messages = client.messages(session_id)
        except Exception:  # noqa: BLE001
            continue
        for message in reversed(messages):
            info = message.get("info", {})
            if info.get("role") == "assistant" and info.get("modelID"):
                result[role] = f"{info.get('providerID')}/{info.get('modelID')}"
                break
    return result


def models_text() -> str:
    configured = team.agent_config_models()
    lines = ["Модели агентов (opencode.json):"]
    for profile in ALL_MODEL_TARGETS:
        lines.append(f"  {profile:<18} {configured.get(profile) or '(не задана)'}")
    used = last_used_models()
    if used:
        lines.append("")
        lines.append("Последние фактические модели сессий:")
        for role, model in used.items():
            lines.append(f"  {role}: {model}")
    server = team.load_server()
    if server:
        lines.append("")
        lines.append(
            f"Сервер загружен {time.strftime('%H:%M:%S', time.localtime(server.get('started', 0)))}; "
            f"конфиг изменён {time.strftime('%H:%M:%S', time.localtime(team.config_mtime()))}"
        )
        if team.config_mtime() > float(server.get("started") or 0):
            lines.append(
                "Конфиг новее сервера: на TUI-сессиях действует кэш; /setmodel применит изменения."
            )
    return "\n".join(lines)


def _sessions_quiescent() -> bool:
    if not DB_PATH.exists():
        return True
    try:
        client = get_client(ensure=False)
    except Exception:  # noqa: BLE001
        return False
    for session_id in _session_ids().values():
        try:
            messages = client.messages(session_id)
        except Exception:  # noqa: BLE001
            return False
        if not messages:
            continue
        info = messages[-1].get("info", {})
        if info.get("role") != "assistant":
            return False
        if not (info.get("time") or {}).get("completed"):
            return False
    return True


def _no_unconsumed_events() -> bool:
    if not DB_PATH.exists():
        return True
    try:
        conn = team_db.connect(DB_PATH)
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM events WHERE consumed=0").fetchone()
            return int(row["n"]) == 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _open_human_question() -> bool:
    if not DB_PATH.exists():
        return False
    try:
        conn = team_db.connect(DB_PATH)
        try:
            return bool(team_db.open_questions(conn, "human"))
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def safe_window_ready() -> bool:
    """True when no agent turn is in flight and no event is waiting to be routed.

    /instance/dispose silently cuts an in-flight turn, so we never reload during one.
    A waiting human question is a reliable idle point; otherwise require a sustained idle.
    """
    if not run_control.is_active():
        return True
    if not _sessions_quiescent() or not _no_unconsumed_events():
        return False
    if _open_human_question():
        return True
    time.sleep(1.5)
    if not run_control.is_active():
        return True
    return _sessions_quiescent() and _no_unconsumed_events()


async def schedule_apply(bot: Bot, chat_id: int) -> None:
    APPLY["dirty"] = True
    task = APPLY.get("task")
    if task is None or task.done():
        APPLY["task"] = asyncio.create_task(apply_worker(bot, chat_id))


async def apply_worker(bot: Bot, chat_id: int) -> None:
    notified = False
    while APPLY["dirty"]:
        APPLY["dirty"] = False
        if await asyncio.to_thread(run_control.is_active):
            ready = await asyncio.to_thread(safe_window_ready)
            if not ready:
                APPLY["dirty"] = True
                if not notified:
                    await bot.send_message(
                        chat_id,
                        "⏳ Жду безопасное окно: перезагружу конфиг без остановки прогона и применю модели.",
                    )
                    notified = True
                await asyncio.sleep(3)
                continue
        server = await asyncio.to_thread(team.load_server)
        tools_stale = bool(server) and (
            await asyncio.to_thread(team.tools_mtime) > float(server.get("started") or 0)
        )
        APPLY["restarting"] = True
        try:
            if tools_stale:
                ok, message = await asyncio.to_thread(team.restart_server_keep_auth)
            else:
                ok, message = await asyncio.to_thread(team.reload_config)
                if not ok:
                    ok2, message2 = await asyncio.to_thread(team.restart_server_keep_auth)
                    if ok2:
                        ok, message = True, message2
                    else:
                        message = f"{message}; перезапуск тоже не удался: {message2}"
        except Exception as exc:  # noqa: BLE001
            ok, message = False, str(exc)
        finally:
            APPLY["restarting"] = False
        if ok:
            clear_pending()
        await bot.send_message(chat_id, ("✅ " if ok else "⚠️ ") + message)
        notified = False
    APPLY["task"] = None


# ------------------------------------------------------------------- questions UI

def question_keyboard(question) -> InlineKeyboardMarkup | None:
    qid = question["id"]
    if question["kind"] in ("approve_controller_plan", "accept_final"):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять", callback_data=f"q:{qid}:approve"),
                    InlineKeyboardButton(text="✏️ Доработать", callback_data=f"q:{qid}:revise"),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Ответить", callback_data=f"q:{qid}:answer")]
        ]
    )


async def send_question(bot: Bot, chat_id: int, question) -> None:
    header = KIND_RU.get(question["kind"], question["kind"])
    text = f"Вопрос q{question['id']} · {header}\n\n{question['text']}"
    chunks = split_text(text)
    keyboard = question_keyboard(question)
    for index, chunk in enumerate(chunks):
        sent = await bot.send_message(
            chat_id, chunk, reply_markup=keyboard if index == len(chunks) - 1 else None
        )
        question_messages[sent.message_id] = int(question["id"])


# --------------------------------------------------------------------- middleware

class Allowlist(BaseMiddleware):
    def __init__(self, chat_id: int):
        self.chat_id = chat_id

    async def __call__(self, handler, event, data):
        chat = None
        if isinstance(event, Message):
            chat = event.chat.id
        elif isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat.id
        if chat != self.chat_id:
            logging.warning("ignored update from chat %s", chat)
            return None
        return await handler(event, data)


# ------------------------------------------------------------------------ handlers

@router.message(CommandStart())
async def h_start(message: Message) -> None:
    await message.answer(
        "Команда AutomateML на связи.\n\n"
        "Управление:\n"
        "/newtask <цель> — запустить прогон\n"
        "/status — состояние\n"
        "/tasks — задачи\n"
        "/events [n] — последние события (метаданные)\n"
        "/questions — открытые вопросы\n"
        "/answer qN <текст> — ответить\n"
        "/cancel — отменить прогон\n"
        "/models — модели агентов\n"
        "/setmodel <target> <provider/model> — сменить модель\n\n"
        "Любой обычный текст — вопрос секретарю. Он видит состояние и историю, "
        "может отвечать на вопросы, запускать/отменять прогоны и передавать указания контролёру."
    )


@router.message(Command("help"))
async def h_help(message: Message) -> None:
    await h_start(message)


@router.message(Command("newtask"))
async def h_newtask(message: Message, command: CommandObject) -> None:
    if APPLY["restarting"]:
        await message.answer("Сейчас применяю модели (перезапуск сервера). Повторите через несколько секунд.")
        return
    task = (command.args or "").strip()
    if not task:
        await message.answer("Использование: /newtask <цель задачи>")
        return
    try:
        result = await asyncio.to_thread(run_control.start_run, task)
    except run_control.RunBusy:
        await message.answer(
            "Прогон уже выполняется. Новый не запущен: дождитесь завершения или /cancel."
        )
        return
    except run_control.RunError as exc:
        await message.answer(f"Не удалось запустить: {exc}")
        return
    await message.answer(
        f"🚀 Запущен новый прогон (pid {result.get('pid')}).\n"
        "Следите за прогрессом здесь; вопросы будут приходить с кнопками."
    )


@router.message(Command("status"))
async def h_status(message: Message) -> None:
    text = await asyncio.to_thread(status_text)
    for chunk in split_text(text):
        await message.answer(chunk)


@router.message(Command("tasks"))
async def h_tasks(message: Message) -> None:
    text = await asyncio.to_thread(tasks_text)
    for chunk in split_text(text):
        await message.answer(chunk)


@router.message(Command("events"))
async def h_events(message: Message, command: CommandObject) -> None:
    try:
        limit = int((command.args or "").strip() or 15)
    except ValueError:
        limit = 15
    limit = max(1, min(limit, 50))
    text = await asyncio.to_thread(events_text, limit)
    for chunk in split_text(text):
        await message.answer(chunk)


@router.message(Command("questions"))
async def h_questions(message: Message) -> None:
    rows = await asyncio.to_thread(open_human_questions)
    if rows is None:
        await message.answer("База состояния сейчас обновляется. Повторите через секунду.")
        return
    if not rows:
        await message.answer("Открытых вопросов нет" if DB_PATH.exists() else "Активного прогона нет.")
        return
    for question in rows:
        await send_question(message.bot, message.chat.id, question)


@router.message(Command("answer"))
async def h_answer(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /answer qN <текст>")
        return
    qid, text = parts
    ok, text_out = await asyncio.to_thread(answer_human, qid, text)
    await message.answer(("✅ " if ok else "") + text_out)


@router.message(Command("cancel"))
async def h_cancel(message: Message) -> None:
    if not await asyncio.to_thread(run_control.is_active):
        await message.answer("Активного прогона нет.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, отменить", callback_data="cancel:yes"),
                InlineKeyboardButton(text="Нет", callback_data="cancel:no"),
            ]
        ]
    )
    await message.answer("Отменить текущий прогон?", reply_markup=keyboard)


@router.message(Command("ask"))
async def h_ask(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer("Использование: /ask <вопрос секретарю>")
        return
    await ask_secretary(message, text)


@router.message(Command("models"))
async def h_models(message: Message) -> None:
    text = await asyncio.to_thread(models_text)
    for chunk in split_text(text):
        await message.answer(chunk)


@router.message(Command("setmodel"))
async def h_setmodel(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(
            "Использование: /setmodel <target> <provider/model>\n"
            "target: controller-plan … tester-build, secretary, controller|researcher|developer|tester, all\n"
            "Текущие модели: /models"
        )
        return
    target, model = parts
    profiles = expand_targets(target)
    if not profiles:
        await message.answer(f"Неизвестная цель «{target}». Список: /models")
        return
    if not team.parse_model_id(model):
        await message.answer("Модель должна быть в формате provider/model, например anymodel/glm-5.3-flash")
        return
    await message.answer("Проверяю модель живым запросом…")
    ok, detail = await asyncio.to_thread(live_model_check, model)
    if not ok:
        await message.answer(f"❌ Модель не принята: {detail}\nКонфиг не изменён.")
        return
    try:
        await asyncio.to_thread(write_model_config, profiles, model)
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"❌ Не удалось записать конфиг: {exc}")
        return
    role_profiles = [profile for profile in profiles if profile != SECRETARY_PROFILE]
    lines = [
        f"✅ Модель {model} принята ({detail}).",
        "Обновлены профили: " + ", ".join(profiles) + ".",
    ]
    if SECRETARY_PROFILE in profiles:
        lines.append("Секретарь перейдёт на неё со следующего сообщения.")
    active = await asyncio.to_thread(run_control.is_active)
    if role_profiles:
        if active:
            lines.append("Роли применятся со следующего хода; конфиг перезагружу в безопасном окне.")
        else:
            lines.append("Активного прогона нет — перезагружаю конфиг, чтобы применить везде.")
    await message.answer("\n".join(lines))
    if role_profiles or not active:
        await asyncio.to_thread(mark_pending, profiles, model)
        await schedule_apply(message.bot, message.chat.id)


@router.callback_query(F.data.startswith("q:"))
async def cb_question(callback: CallbackQuery) -> None:
    if APPLY["restarting"]:
        await callback.answer("Идёт перезапуск сервера, повторите через пару секунд", show_alert=True)
        return
    try:
        _, qid_text, action = (callback.data or "").split(":")
        qid = int(qid_text)
    except ValueError:
        await callback.answer("Некорректная кнопка")
        return
    if action == "approve":
        ok, text = await asyncio.to_thread(answer_human, qid, "approve")
        await callback.answer(text[:190], show_alert=not ok)
        if ok and callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001 - message may be old
                pass
        return
    prompt = (
        "Напишите замечания ответом (reply) на это сообщение."
        if action == "revise"
        else "Напишите ответ ответом (reply) на это сообщение."
    )
    if callback.message:
        sent = await callback.message.answer(prompt)
        await_reply[sent.message_id] = qid
    await callback.answer()


@router.callback_query(F.data.in_({"cancel:yes", "cancel:no"}))
async def cb_cancel(callback: CallbackQuery) -> None:
    if APPLY["restarting"]:
        await callback.answer("Идёт перезапуск сервера, повторите через пару секунд", show_alert=True)
        return
    if callback.data == "cancel:no":
        await callback.answer("Отмена отменена")
        if callback.message:
            await callback.message.edit_text("Ок, продолжаем.")
        return
    await callback.answer("Отмена запрошена")
    stopped = await asyncio.to_thread(run_control.cancel_run)
    if callback.message:
        await callback.message.edit_text(
            "🛑 Отмена запрошена." if stopped else "Активного прогона уже нет."
        )


@router.message(F.text)
async def h_text(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    if APPLY["restarting"]:
        await message.answer("Сейчас применяю модели (перезапуск сервера). Повторите через несколько секунд.")
        return
    qid = None
    if message.reply_to_message:
        replied = message.reply_to_message.message_id
        qid = await_reply.get(replied) or question_messages.get(replied)
    if qid is not None:
        ok, out = await asyncio.to_thread(answer_human, qid, text)
        if ok:
            await_reply.pop(message.reply_to_message.message_id, None)
        await message.answer(("✅ " if ok else "") + out)
        return
    await ask_secretary(message, text)


# ------------------------------------------------------------------- progress loop

async def progress_loop(bot: Bot, chat_id: int) -> None:
    state = load_state()
    while True:
        try:
            task = APPLY.get("task")
            if model_admin.pending_exists() and (task is None or task.done()) and not APPLY["restarting"]:
                await schedule_apply(bot, chat_id)
            snapshot = await asyncio.to_thread(db_snapshot, int(state.get("last_event_id") or 0))
            await apply_snapshot(bot, chat_id, state, snapshot)
        except Exception:  # noqa: BLE001 - keep the bot alive
            logging.exception("progress loop failed")
        await asyncio.sleep(2)


async def apply_snapshot(bot: Bot, chat_id: int, state: dict, snapshot: dict) -> None:
    if snapshot.get("no_db"):
        if state.get("run_tag"):
            state["run_tag"] = ""
            state["last_event_id"] = 0
            state["sent_questions"] = []
            state["run_status"] = ""
            question_messages.clear()
            await_reply.clear()
            save_state(state)
        return

    tag = snapshot["run_tag"]
    if not state.get("initialized"):
        state["initialized"] = True
        state["run_tag"] = tag
        state["last_event_id"] = int(snapshot["max_event"])
        state["run_status"] = snapshot["run_status"]
        state["sent_questions"] = []
        save_state(state)
        return

    if tag and tag != state.get("run_tag"):
        task = (snapshot.get("task") or "").strip()
        await bot.send_message(
            chat_id,
            "🆕 Запущен новый прогон." + (f"\nЗадача: {task[:300]}" if task else ""),
        )
        state["run_tag"] = tag
        state["last_event_id"] = 0
        state["sent_questions"] = []
        state["run_status"] = "running"
        question_messages.clear()
        await_reply.clear()

    for event in snapshot["events"]:
        line = event_line(event)
        if line:
            try:
                await bot.send_message(chat_id, line)
            except Exception:  # noqa: BLE001
                logging.exception("failed to send event line")
        state["last_event_id"] = int(event["id"])

    sent = set(int(x) for x in state.get("sent_questions", []))
    for question in snapshot["questions"]:
        qid = int(question["id"])
        if qid in sent:
            continue
        await send_question(bot, chat_id, question)
        sent.add(qid)
    state["sent_questions"] = sorted(sent)

    status = snapshot["run_status"]
    previous = state.get("run_status")
    if status in ("done", "cancelled") and previous == "running":
        note = "🏁 Прогон завершён." if status == "done" else "🛑 Прогон отменён."
        await bot.send_message(chat_id, note)
    if status == "running" and not snapshot.get("active") and previous == "running":
        await bot.send_message(
            chat_id,
            "⚠️ Оркестратор не активен, а статус всё ещё running. "
            "Возможно, прогон упал — спросите секретаря или посмотрите run/console.log.",
        )
        status = "unknown"
    state["run_status"] = status
    save_state(state)


# ---------------------------------------------------------------------------- main

async def run_bot(token: str, chat_id: int) -> None:
    try:
        await asyncio.to_thread(prepare_secretary)
    except Exception:  # noqa: BLE001 - commands still work
        logging.exception("could not prepare secretary session at startup")

    bot = Bot(token, default=DefaultBotProperties(parse_mode=None))
    me = await bot.get_me()
    logging.info("bot @%s started, chat_id=%s", me.username, chat_id)
    print(f"Бот @{me.username} запущен; разрешённый чат: {chat_id}")

    dispatcher = Dispatcher()
    dispatcher.message.middleware(Allowlist(chat_id))
    dispatcher.callback_query.middleware(Allowlist(chat_id))
    dispatcher.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(command="newtask", description="запустить прогон"),
            BotCommand(command="status", description="состояние"),
            BotCommand(command="tasks", description="задачи"),
            BotCommand(command="events", description="последние события"),
            BotCommand(command="questions", description="открытые вопросы"),
            BotCommand(command="answer", description="ответить на вопрос"),
            BotCommand(command="cancel", description="отменить прогон"),
            BotCommand(command="models", description="модели агентов"),
            BotCommand(command="setmodel", description="сменить модель агента"),
            BotCommand(command="ask", description="спросить секретаря"),
        ]
    )

    asyncio.create_task(progress_loop(bot, chat_id))
    if PENDING_FILE.exists():
        await bot.send_message(
            chat_id,
            "Есть неприменённые изменения моделей — применю в безопасном окне.",
        )
        await schedule_apply(bot, chat_id)
    await dispatcher.start_polling(bot, drop_pending_updates=True)


def cmd_whoami(token: str) -> int:
    try:
        with urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as response:
            data = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка Telegram API: {exc}", file=sys.stderr)
        return 1
    chats: dict[int, str] = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if chat.get("id"):
            chats[int(chat["id"])] = (
                chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
            )
    if not chats:
        print("Нет сообщений боту. Напишите боту /start и повторите.")
        return 0
    for cid, name in chats.items():
        print(f"chat_id={cid}  ({name})")
    return 0


def cmd_check(token: str) -> int:
    try:
        with urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=20) as response:
            data = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка Telegram API: {exc}", file=sys.stderr)
        return 1
    if not data.get("ok"):
        print(f"Telegram ответил ошибкой: {data}", file=sys.stderr)
        return 1
    user = data["result"]
    print(f"Токен валиден: @{user.get('username')} ({user.get('first_name')})")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="tg_bot.py")
    parser.add_argument("--check", action="store_true", help="проверить токен")
    parser.add_argument("--whoami", action="store_true", help="показать chat_id по сообщениям боту")
    args = parser.parse_args(argv)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    )

    token, chat_id = load_config()
    if not token:
        print(
            "Не задан TELEGRAM_BOT_TOKEN (env или run/telegram.json).\n"
            "Файл: {\"token\": \"...\", \"chat_id\": 123456789} с правами 600.",
            file=sys.stderr,
        )
        return 2
    if args.check:
        return cmd_check(token)
    if args.whoami:
        return cmd_whoami(token)
    if not chat_id:
        print(
            "Не задан TELEGRAM_ALLOWED_CHAT_ID (env или run/telegram.json).\n"
            "Подсказка: python3 tg_bot.py --whoami",
            file=sys.stderr,
        )
        return 2
    try:
        asyncio.run(run_bot(token, chat_id))
    except KeyboardInterrupt:
        print("\nБот остановлен.")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))