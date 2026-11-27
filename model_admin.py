"""Model administration shared by the Telegram bot and the secretary tool.

The opencode server caches agent models at startup; a change is applied by writing
opencode.json and asking the server to reload the instance (POST /instance/dispose).
That reload silently cuts an in-flight turn, so the bot performs it only in a safe
window. The secretary only writes the config and marks a pending change; the bot's
worker applies it (or the next run picks it up).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import team

PROJECT = Path(__file__).resolve().parent
RUN_DIR = PROJECT / "run"
PENDING_FILE = RUN_DIR / "models_pending.json"
SECRETARY_PROFILE = "secretary"
ROLE_PROFILES = [
    f"{role}-{phase}"
    for role in ("controller", "researcher", "developer", "tester")
    for phase in ("plan", "build")
]
SHORT_ROLES = ("controller", "researcher", "developer", "tester")
ALL_MODEL_TARGETS = ROLE_PROFILES + [SECRETARY_PROFILE]


def expand_targets(target: str) -> list[str] | None:
    target = (target or "").strip().lower()
    if target in ALL_MODEL_TARGETS:
        return [target]
    if target in SHORT_ROLES:
        return [f"{target}-plan", f"{target}-build"]
    if target in ("all", "*"):
        return list(ALL_MODEL_TARGETS)
    return None


def config_models() -> dict[str, str]:
    return team.agent_config_models()


def write_config(profiles: list[str], model: str, path: Path | None = None) -> None:
    path = Path(path) if path else (PROJECT / "opencode.json")
    original = path.read_text("utf-8")
    try:
        (RUN_DIR / "opencode.json.bak").write_text(original, "utf-8")
    except OSError:
        pass
    data = json.loads(original)
    for profile in profiles:
        data.setdefault("agent", {}).setdefault(profile, {})["model"] = model
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, path)
    team.MODEL_CACHE["mtime"] = None


def mark_pending(profiles: list[str], model: str) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(
        json.dumps(
            {"profiles": profiles, "model": model, "ts": time.time()},
            ensure_ascii=False,
        ),
        "utf-8",
    )


def pending_exists() -> bool:
    return PENDING_FILE.exists()


def pending_info() -> dict:
    try:
        return json.loads(PENDING_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def clear_pending() -> None:
    try:
        PENDING_FILE.unlink()
    except FileNotFoundError:
        pass


def live_check(model: str) -> tuple[bool, str]:
    """One tiny prompt to a scratch session with the explicit model. No server restart."""
    parsed = team.parse_model_id(model)
    if not parsed:
        return False, "нужен формат provider/model"
    server = team.load_server()
    if not server:
        return False, "opencode server не запущен"
    client = team.OpenCode(server["url"], server.get("password"), timeout=180)
    if not client.health():
        return False, "opencode server недоступен"
    session_id = None
    try:
        session_id = client.create_session("team/model-check")
        result = client.prompt(
            session_id,
            "plan",
            "Ответь ровно одним словом: ок. Не вызывай инструменты.",
            attempts=1,
            stall_timeout=90,
            model=parsed,
        )
        info = (result or {}).get("info") or {}
        used = f"{info.get('providerID')}/{info.get('modelID')}"
        if info.get("error"):
            return False, f"провайдер вернул ошибку ({used or model})"
        return True, used or "(модель не указана в ответе)"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        if session_id:
            try:
                client.request("DELETE", f"/session/{quote(session_id, safe='')}", timeout=30)
            except Exception:  # noqa: BLE001
                pass


def models_text(used: dict[str, str] | None = None) -> str:
    configured = config_models()
    lines = ["Модели агентов (opencode.json):"]
    for profile in ALL_MODEL_TARGETS:
        lines.append(f"  {profile:<18} {configured.get(profile) or '(не задана)'}")
    if used:
        lines.append("")
        lines.append("Последние фактические модели сессий:")
        for role, model in used.items():
            lines.append(f"  {role}: {model}")
    pending = pending_info()
    if pending:
        lines.append("")
        lines.append(
            "⏳ Есть неприменённое изменение: "
            + ", ".join(pending.get("profiles") or [])
            + " → "
            + str(pending.get("model"))
            + " (применится в безопасном окне)."
        )
    return "\n".join(lines)