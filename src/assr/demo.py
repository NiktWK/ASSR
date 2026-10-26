"""Generate an explicit local demo profile and data, never executed in live mode."""

from pathlib import Path
import random

from .models import digest
from .workspace import write_json


def setup(root: Path, example: Path):
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)

    def rows(prefix, n):
        result = []
        for i in range(n):
            x = [rng.uniform(-2, 2), rng.uniform(-2, 2)]
            result.append({"id": f"{prefix}-{i}", "x": x, "label": int(2 * x[0] - x[1] > 0)})
        return result

    train, val = rows("train", 80), rows("val", 40)
    data = {"train.json": train, "validation.json": [{"id": r["id"], "x": r["x"]} for r in val]}
    for name, value in data.items():
        write_json(root / "data" / name, value)
    labels = [{"id": r["id"], "label": r["label"]} for r in val]
    write_json(root / "labels.json", labels)
    files = {"train.py": (example / "train.py").read_text()}
    from . import evaluator

    evaluator_text = Path(evaluator.__file__).read_text()
    profile = {
        "files": files,
        "dataset_digest": digest(data),
        "evaluator_digest": digest({"source": evaluator_text, "labels": labels}),
        "image_digest": "demo-local-python",
        "argv": ["python", "/work/train.py"],
        "smoke_argv": ["python", "/work/train.py"],
        "data_dir": str((root / "data").resolve()),
        "labels_path": str((root / "labels.json").resolve()),
        "allowed_files": ["train.py"],
    }
    profile.pop("allowed_files")
    write_json(root / "profiles" / "toy.json", profile)
    return profile
