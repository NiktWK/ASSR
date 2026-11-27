"""Runtime settings shared by the bot, the orchestrator and the secretary tool.

Currently: per-role context-compaction thresholds (tokens). The file is re-read on
every check, so a change made in the bot applies to the next turn without a restart.
A threshold of 0 (or a missing role) disables automatic compaction for that role.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
RUN_DIR = PROJECT / "run"
SETTINGS_FILE = RUN_DIR / "settings.json"

DEFAULT_COMPACT = {
    "controller": 150_000,
    "researcher": 200_000,
    "developer": 200_000,
    "tester": 200_000,
    "secretary": 150_000,
}
COMPACT_ROLES = tuple(DEFAULT_COMPACT)
MIN_TOKENS = 10_000
MAX_TOKENS = 2_000_000


def load() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _stored_compact() -> dict:
    compact = load().get("compact")
    return compact if isinstance(compact, dict) else {}


def _normalize(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if MIN_TOKENS <= number <= MAX_TOKENS else 0


def compact_thresholds() -> dict[str, int]:
    """role -> token threshold (0 = off); defaults fill the gaps."""
    stored = _stored_compact()
    result = dict(DEFAULT_COMPACT)
    for role in COMPACT_ROLES:
        if role in stored:
            result[role] = _normalize(stored[role])
    return result


def compact_threshold(role: str) -> int:
    return compact_thresholds().get(role, 0)


def expand_targets(target: str) -> list[str] | None:
    target = (target or "").strip().lower()
    if target in COMPACT_ROLES:
        return [target]
    if target in ("all", "*"):
        return list(COMPACT_ROLES)
    return None


def validate_tokens(value) -> int | None:
    """None when invalid; 0 means "off"."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number == 0:
        return 0
    if MIN_TOKENS <= number <= MAX_TOKENS:
        return number
    return None


def set_compact(target: str, value) -> list[str]:
    roles = expand_targets(target)
    if not roles:
        raise ValueError(f"неизвестная роль «{target}»")
    number = validate_tokens(value)
    if number is None:
        raise ValueError(f"порог: 0 (выключить) или {MIN_TOKENS}…{MAX_TOKENS} токенов")
    data = load()
    compact = data.get("compact")
    if not isinstance(compact, dict):
        compact = {}
    for role in roles:
        compact[role] = number
    data["compact"] = compact
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, SETTINGS_FILE)
    return roles


def format_tokens(value: int | None) -> str:
    if not value:
        return "выкл"
    if value >= 1000:
        return f"{value / 1000:g}k"
    return str(value)
