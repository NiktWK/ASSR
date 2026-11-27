#!/usr/bin/env python3
"""Backend of the `team` opencode tool.

The agents never talk to each other directly: they call the `team` tool, which runs this
script. The script only validates identity/phase and records an event in the state DB;
the orchestrator (team.py) does the routing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import team_db


class Reject(Exception):
    pass


def _phase_of(conn, role: str) -> str:
    phase = team_db.get_phase(conn, role)
    if not phase:
        raise Reject(f"роль {role} ещё не имеет активной фазы")
    return phase


def _task_for(conn, role: str, task_id: str, owned: bool = True):
    if not task_id:
        raise Reject("нужен task_id")
    task = team_db.get_task(conn, task_id)
    if not task:
        raise Reject(f"задача {task_id} не найдена")
    if owned and task["worker"] != role:
        raise Reject(f"задача {task_id} принадлежит роли {task['worker']}, а не {role}")
    return task


def do_submit(conn, role: str, args) -> str:
    phase = _phase_of(conn, role)
    if args.phase != phase:
        raise Reject(f"сейчас фаза {phase}, а submit пришёл для {args.phase}")
    if team_db.is_submitted(conn, role):
        raise Reject("результат этой фазы уже сдан — ждите вердикт координатора")
    if not (args.content or "").strip():
        raise Reject("пустой content")

    task_id = args.task_id or ""
    if role in ("developer", "tester"):
        task = _task_for(conn, role, task_id)
        expected = f"{phase}_pending"
        if task["status"] != expected:
            raise Reject(
                f"задача {task_id} в статусе {task['status']}, submit для фазы {phase} сейчас не ожидается"
            )
        team_db.set_task_status(conn, task_id, f"{phase}_review")
    elif task_id:
        raise Reject("контролёр и исследователь не сдают задачи с task_id")

    team_db.mark_submitted(conn, role)
    team_db.add_event(
        conn,
        role,
        "submit",
        {
            "phase": phase,
            "content": args.content,
            "task_id": task_id or None,
        },
    )
    if role in ("developer", "tester"):
        return f"Результат фазы {phase} по задаче {task_id} принят. Ждите вердикт исследователя."
    return f"Результат фазы {phase} принят. Ждите вердикт координатора."


def do_review(conn, role: str, args) -> str:
    if role not in ("researcher", "controller"):
        raise Reject("ревью доступно только исследователю и контролёру")
    if args.phase not in team_db.PHASES:
        raise Reject("для review нужен phase=plan|build")
    if args.verdict not in ("approve", "revise"):
        raise Reject("verdict должен быть approve или revise")
    if args.verdict == "revise" and not (args.comments or "").strip():
        raise Reject("для revise нужны конкретные comments")

    task_id = args.task_id or ""
    if role == "researcher":
        task = _task_for(conn, role, task_id, owned=False)
        expected = f"{args.phase}_review"
        if task["status"] != expected:
            raise Reject(
                f"задача {task_id} в статусе {task['status']}, а не {expected}"
            )
    else:
        if task_id:
            raise Reject("контролёр проверяет исследователя без task_id")
        researcher_phase = team_db.get_phase(conn, "researcher")
        if researcher_phase != args.phase:
            raise Reject(
                f"исследователь сейчас в фазе {researcher_phase}, а не в {args.phase}"
            )
        if not team_db.is_submitted(conn, "researcher"):
            raise Reject("исследователь ещё не сдал этот результат на проверку")

    team_db.add_event(
        conn,
        role,
        "review",
        {
            "task_id": task_id or None,
            "phase": args.phase,
            "verdict": args.verdict,
            "comments": args.comments or "",
        },
    )
    return f"Вердикт {args.verdict} по фазе {args.phase} принят."


def do_assign(conn, role: str, args) -> str:
    if role != "researcher":
        raise Reject("assign доступен только исследователю")
    if _phase_of(conn, role) != "build":
        raise Reject("assign доступен только в фазе BUILD")
    if args.worker not in ("developer", "tester"):
        raise Reject("worker должен быть developer или tester")
    if not (args.goal or "").strip():
        raise Reject("нужна цель задачи (goal)")
    active = team_db.active_task_for(conn, args.worker)
    if active:
        raise Reject(
            f"у {args.worker} уже есть активная задача {active['id']} ({active['status']}); "
            "дождитесь её закрытия"
        )
    task_id = team_db.new_task(conn, args.worker, args.goal.strip(), args.context or "")
    team_db.add_event(
        conn,
        role,
        "assign",
        {
            "task_id": task_id,
            "worker": args.worker,
            "goal": args.goal.strip(),
            "context": args.context or "",
        },
    )
    return f"Задача {task_id} для {args.worker} создана и передана исполнителю."


def do_ask(conn, role: str, args) -> str:
    if not (args.question or "").strip():
        raise Reject("пустой вопрос")
    to_role = team_db.SUPERVISOR[role]
    phase = team_db.get_phase(conn, role)
    qid = team_db.new_question(
        conn,
        from_role=role,
        to_role=to_role,
        kind="agent",
        text=args.question.strip(),
        phase=phase,
    )
    team_db.add_event(
        conn, role, "ask", {"question_id": qid, "to_role": to_role}
    )
    who = "человеку" if to_role == "human" else to_role
    return (
        f"Вопрос q{qid} отправлен {who}. Твой ход завершён; ответ придёт новым сообщением. "
        "Не повторяй вопрос."
    )


def do_answer(conn, role: str, args) -> str:
    question = team_db.get_question(conn, args.question_id)
    if not question:
        raise Reject(f"вопрос {args.question_id} не найден")
    if question["status"] != "open":
        raise Reject(f"вопрос q{question['id']} уже закрыт")
    if question["to_role"] != role:
        raise Reject(
            f"на вопрос q{question['id']} отвечает {question['to_role']}, а не {role}"
        )
    if not (args.answer or "").strip():
        raise Reject("пустой ответ")
    team_db.answer_question(conn, question["id"], args.answer.strip())
    team_db.add_event(
        conn,
        role,
        "answer",
        {"question_id": question["id"], "text": args.answer.strip(), "from_role": role},
    )
    return f"Ответ на вопрос q{question['id']} отправлен автору."


def do_sources(conn, role: str, args) -> str:
    if role != "researcher":
        raise Reject("sources доступен только исследователю")
    if not (args.url or "").strip():
        raise Reject("нужен url")
    team_db.add_source(conn, role, args.url.strip(), args.note or "")
    team_db.add_event(
        conn, role, "sources", {"url": args.url.strip(), "note": args.note or ""}
    )
    total = len(team_db.list_sources(conn))
    return f"Источник записан (всего {total})."


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="team_agent.py")
    parser.add_argument("--session", required=True)
    parser.add_argument("--agent", default="")
    parser.add_argument("action", choices=["submit", "review", "assign", "ask", "answer", "sources", "status"])
    parser.add_argument("--phase")
    parser.add_argument("--content")
    parser.add_argument("--task-id")
    parser.add_argument("--verdict")
    parser.add_argument("--comments")
    parser.add_argument("--worker")
    parser.add_argument("--goal")
    parser.add_argument("--context")
    parser.add_argument("--question")
    parser.add_argument("--question-id")
    parser.add_argument("--answer")
    parser.add_argument("--url")
    parser.add_argument("--note")
    args = parser.parse_args(argv)

    path = team_db.db_path()
    if not Path(path).exists():
        print(f"ОШИБКА: база состояния не найдена: {path}", file=sys.stderr)
        return 2
    conn = team_db.connect(path)
    try:
        role = team_db.role_of_session(conn, args.session)
        if not role:
            raise Reject(
                "эта opencode-сессия не зарегистрирована в команде; "
                "запустите оркестратор team.py"
            )
        if args.agent and not args.agent.startswith(role):
            raise Reject(f"агент {args.agent} не соответствует роли сессии {role}")

        handlers = {
            "submit": do_submit,
            "review": do_review,
            "assign": do_assign,
            "ask": do_ask,
            "answer": do_answer,
            "sources": do_sources,
        }
        if args.action == "status":
            print(team_db.summary(conn))
            return 0
        print(handlers[args.action](conn, role, args))
        return 0
    except Reject as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))