"""Only profile allowlisted UTF-8 files can be changed by the coding agent."""

import json
import os
from pathlib import Path, PurePosixPath
import subprocess


def safe_files(files, allowed):
    if not files or len(files) > 50 or sum(len(v.encode()) for v in files.values()) > 500_000:
        raise ValueError("empty or oversized source bundle")
    for name, content in files.items():
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or str(p) != name or name not in allowed:
            raise ValueError(f"path is not allowlisted: {name}")
        if "\x00" in content:
            raise ValueError("source contains NUL")
    return files


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, text=True, capture_output=True, timeout=30
    ).stdout.strip()


def commit_bundle(root: Path, files: dict, label: str) -> str:
    """A separate local evidence repository; never pushes to a user's main branch."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        git(root, "init", "-q")
        git(root, "config", "user.email", "assr@localhost")
        git(root, "config", "user.name", "ASSR")
    for name, text in files.items():
        p = root / name
        if p.is_symlink() or any(x.is_symlink() for x in p.parents if x != root.parent):
            raise ValueError("symlink rejected")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "add", "--all")
    git(root, "commit", "-q", "--allow-empty", "-m", label)
    return git(root, "rev-parse", "HEAD")


def load_profile(root: Path, name: str) -> dict:
    path = root / f"{name}.json"
    data = json.loads(path.read_text())
    data["allowed_files"] = list(data["files"])
    safe_files(data["files"], data["allowed_files"])
    for key in ("dataset_digest", "evaluator_digest", "image_digest", "argv", "smoke_argv"):
        if key not in data:
            raise ValueError(f"missing profile field {key}")
    return data


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
