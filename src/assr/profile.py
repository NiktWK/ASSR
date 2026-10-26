"""Administrator-side onboarding of a fixed Git project and an evaluator dataset."""

import argparse
import json
import subprocess
from pathlib import Path

from . import evaluator
from .models import digest
from .runner import validate_data
from .workspace import git, safe_files, write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--ref", required=True, help="Full immutable upstream commit SHA")
    p.add_argument("--file", action="append", required=True, help="Allowlisted source path, repeatable")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--image", required=True, help="Training image name@sha256:digest")
    p.add_argument("--evaluator-image", required=True)
    p.add_argument("--command", default='["python", "/work/train.py"]', help="JSON argv, no shell")
    p.add_argument("--smoke-command", default='["python", "/work/train.py"]')
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    resolved = git(a.repo, "rev-parse", a.ref + "^{commit}")
    if resolved != a.ref:
        p.error("--ref must be the complete immutable commit SHA")
    # Validate paths before passing them to Git, and reject symlink/tree modes.
    safe_files({name: "placeholder" for name in a.file}, a.file)
    files = {}
    for name in a.file:
        entry = git(a.repo, "ls-tree", a.ref, "--", name)
        if not entry.startswith("100644 blob ") and not entry.startswith("100755 blob "):
            raise ValueError("only regular source files can be included")
        files[name] = subprocess.run(
            ["git", "-C", str(a.repo), "show", a.ref + ":" + name],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    data = {x.name: json.loads(x.read_text()) for x in sorted(a.data.glob("*.json"))}
    labels = json.loads(a.labels.read_text())
    profile = {
        "files": files,
        "upstream_commit": a.ref,
        "upstream_repo": str(a.repo.resolve()),
        "image_digest": a.image,
        "evaluator_image": a.evaluator_image,
        "argv": json.loads(a.command),
        "smoke_argv": json.loads(a.smoke_command),
        "dataset_digest": digest(data),
        "evaluator_digest": digest({"source": Path(evaluator.__file__).read_text(), "labels": labels}),
        "data_dir": str(a.data.resolve()),
        "labels_path": str(a.labels.resolve()),
        "output_quota_verified": False,
        "max_walltime": 3600,
        "max_gpus": 1,
        "memory": "4g",
        "cpus": 2,
    }
    validate_data(profile)
    write_json(a.out, profile)
    print(a.out)
    print(
        "Before live use configure the runner filesystem quota and immutable images. No credentials belong in profiles."
    )


if __name__ == "__main__":
    main()
