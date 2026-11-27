#!/usr/bin/env python3
"""Backend of the `assistant` opencode tool used by the secretary agent.

The secretary may read the whole project state/history with the normal read tools, but
state-changing actions go through this tool. The tool only accepts calls from the
secretary session registered in run/secretary.json.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import model_admin
import run_control
import team
import team_db

PROJECT = Path(__file__).resolve().parent
RUN_DIR = PROJECT / "run"
DB_PATH = RUN_DIR / "state.db"
SECRETARY_FILE = RUN_DIR / "secretary.json"


class Reject(Exception):
    pass


def secretary_session_id() -> str | None:
    try:
        return json.loads(SECRETARY_FILE.read_text("utf-8")).get("session_id")
    except (OSError, json.JSONDecodeError):
        return None


def _open_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise Reject("нет базы прогона: сначала запустите задачу (/newtask)")
    return team_db.connect(DB_PATH)


def _run_status() -> tuple[str, str, str]:
    if not DB_PATH.exists():
        return "нет прогона", "", ""
    conn = team_db.connect(DB_PATH)
    try:
        status = team_db.get_meta(conn, "run_status", "?") or "?"
        tag = team_db.get_meta(conn, "run_tag", "") or ""
        task = team_db.get_meta(conn, "task", "") or ""
        return status, tag, task
    finally:
        conn.close()


def do_status(conn, args) -> str:
    if not DB_PATH.exists():
        return (
            "Активного прогона нет.\n"
            f"Оркестратор запущен: {'да' if run_control.is_active() else 'нет'}."
        )
    status, tag, task = _run_status()
    lines = [
        f"run_status={status}",
        f"run_tag={tag}",
        f"задача: {task[:400]}" if task else "задача: (нет)",
        f"оркестратор: {'активен' if run_control.is_active() else 'не активен'}",
        "",
        team_db.summary(conn),
    ]
    open_q = team_db.open_questions(conn, "human")
    lines.append(f"\nОткрытых вопросов человеку: {len(open_q)}")
    for q in open_q:
        lines.append(f"- q{q['id']} ({q['kind']}): {q['text'][:160]}")
    return "\n".join(lines)


def do_tasks(conn, args) -> str:
    rows = team_db.list_tasks(conn)
    if not rows:
        return "Задач пока нет."
    return "\n".join(
        f"- {r['id']} ({r['worker']}): {r['status']}; цель: {r['goal']}"
        for r in rows
    )


def do_events(conn, args) -> str:
    limit = max(1, min(int(args.limit or 20), 100))
    rows = team_db.recent_events(conn, limit)
    if not rows:
        return "Событий нет."
    return "\n".join(team_db.format_event_meta(r) for r in rows)


def do_questions(conn, args) -> str:
    rows = team_db.open_questions(conn, "human")
    if not rows:
        return "Открытых вопросов человеку нет."
    return "\n\n".join(
        f"q{q['id']} · {q['kind']} · от {q['from_role']}\n{q['text']}" for q in rows
    )


def do_question(conn, args) -> str:
    if not args.id:
        raise Reject("нужен --id (например q3)")
    question = team_db.get_question(conn, args.id)
    if not question:
        raise Reject(f"вопрос {args.id} не найден")
    return (
        f"q{question['id']} · {question['kind']} · {question['from_role']} → "
        f"{question['to_role']} · status={question['status']}\n\n{question['text']}"
    )


def do_answer(conn, args) -> str:
    if not args.id or not (args.text or "").strip():
        raise Reject("нужны --id и --text")
    qid = team_db.answer_human(conn, args.id, args.text)
    return f"Ответ на q{qid} принят и передан оркестратору."


def do_start(conn, args) -> str:
    task = (args.task or args.text or "").strip()
    if not task:
        raise Reject("нужна формулировка задачи (--task)")
    try:
        result = run_control.start_run(task)
    except run_control.RunBusy:
        return "Прогон уже выполняется. Новый не запущен; предложи оператору /cancel."
    except run_control.RunError as exc:
        raise Reject(str(exc)) from None
    tag = result.get("run_tag") or "(тег появится через пару секунд)"
    return f"Новый прогон запущен (pid {result.get('pid')}, run_tag {tag})."


def do_stop(conn, args) -> str:
    if not run_control.is_active():
        return "Активного прогона нет — останавливать нечего."
    run_control.cancel_run()
    return "Отмена прогона запрошена; текущий шаг агента будет прерван."


def do_tell_controller(conn, args) -> str:
    text = (args.text or "").strip()
    if not text:
        raise Reject("нужен текст указания (--text)")
    if not run_control.is_active():
        raise Reject("нет активного прогона: передать указание контролёру некому")
    if conn is None:
        raise Reject("нет базы прогона")
    phase = team_db.get_phase(conn, "controller")
    if phase is None:
        return "Прогон только запускается. Повторите через несколько секунд."
    if phase == "plan":
        return (
            "Сейчас идёт согласование плана (PLAN). Передайте замечания как ответ на вопрос "
            "человеку (/questions) — контролёр учтёт их при доработке плана."
        )
    team_db.add_event(conn, "human", "inject", {"text": text, "target": "controller"})
    return (
        "Указание поставлено в очередь контролёру. Оно будет доставлено после "
        "текущего шага оркестратора."
    )


def do_models(conn, args) -> str:
    text = model_admin.models_text()
    if not run_control.is_active():
        text += "\n\nАктивного прогона нет; изменения применятся при следующем запуске бота/прогона."
    return text


def do_setmodel(conn, args) -> str:
    target = (args.target or "").strip()
    model = (args.model or "").strip()
    if not target or not model:
        raise Reject("нужны --target и --model")
    profiles = model_admin.expand_targets(target)
    if not profiles:
        raise Reject(f"неизвестная цель «{target}»")
    if not team.parse_model_id(model):
        raise Reject("модель должна быть в формате provider/model")
    ok, detail = model_admin.live_check(model)
    if not ok:
        raise Reject(f"модель не принята: {detail}")
    model_admin.write_config(profiles, model)
    model_admin.mark_pending(profiles, model)
    lines = [
        f"Модель {model} записана для: {', '.join(profiles)}.",
        f"Живая проверка: {detail}.",
    ]
    if model_admin.SECRETARY_PROFILE in profiles:
        lines.append("Секретарь перейдёт на неё со следующего сообщения.")
    if run_control.is_active():
        lines.append(
            "Применение к текущему прогону поставлено в очередь: бот перезагрузит конфиг "
            "в безопасном окне, не останавливая прогон."
        )
    else:
        lines.append("Активного прогона нет; изменение применится при следующем запуске бота/прогона.")
    return "\n".join(lines)


def do_result(conn, args) -> str:
    if conn is None:
        return "Финального заключения контролёра пока нет."
    content = team_db.last_final_result(conn)
    if not content:
        return "Финального заключения контролёра пока нет."
    return content


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="assistant_agent.py")
    parser.add_argument("--session", required=True)
    parser.add_argument(
        "action",
        choices=[
            "status",
            "tasks",
            "events",
            "questions",
            "question",
            "answer",
            "start",
            "stop",
            "tell_controller",
            "result",
            "models",
            "setmodel",
        ],
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--id")
    parser.add_argument("--text")
    parser.add_argument("--task")
    parser.add_argument("--target")
    parser.add_argument("--model")
    args = parser.parse_args(argv)

    expected = secretary_session_id()
    if not expected:
        print(
            "ОТКАЗ: секретарь не инициализирован (запустите tg_bot.py, он создаст сессию)",
            file=sys.stderr,
        )
        return 2
    if args.session != expected:
        print("ОТКАЗ: этот инструмент доступен только сессии секретаря", file=sys.stderr)
        return 2

    handlers = {
        "status": do_status,
        "tasks": do_tasks,
        "events": do_events,
        "questions": do_questions,
        "question": do_question,
        "answer": do_answer,
        "start": do_start,
        "stop": do_stop,
        "tell_controller": do_tell_controller,
        "result": do_result,
        "models": do_models,
        "setmodel": do_setmodel,
    }
    handler = handlers[args.action]

    needs_db = args.action in (
        "tasks",
        "events",
        "questions",
        "question",
        "answer",
        "tell_controller",
    )
    conn = None
    try:
        if needs_db:
            conn = _open_db()
        else:
            conn = team_db.connect(DB_PATH) if DB_PATH.exists() else None
        print(handler(conn, args))
        return 0
    except (Reject, ValueError) as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))