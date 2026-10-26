"""Trusted accuracy evaluator. Never imports or unpickles model-produced objects."""

import argparse
import json
import math
from pathlib import Path


def evaluate(predictions, labels):
    if not isinstance(predictions, list) or not predictions or len(predictions) != len(labels):
        raise ValueError("missing or wrong number of predictions")
    if len({x["id"] for x in predictions}) != len(predictions):
        raise ValueError("duplicate prediction ids")
    expected = {x["id"]: x["label"] for x in labels}
    if len(expected) != len(labels) or set(expected) != {x["id"] for x in predictions}:
        raise ValueError("evaluation IDs mismatch")
    for row in predictions:
        value = row["prediction"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("NaN or invalid prediction")
        if value not in (0, 1):
            raise ValueError("binary predictions must be 0 or 1")
    return {
        "accuracy": sum(x["prediction"] == expected[x["id"]] for x in predictions) / len(labels),
        "count": len(labels),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--predictions", required=True)
    p.add_argument("--labels", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    if Path(a.predictions).stat().st_size > 10_000_000:
        raise ValueError("oversized predictions")
    result = evaluate(json.loads(Path(a.predictions).read_text()), json.loads(Path(a.labels).read_text()))
    Path(a.out).write_text(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
