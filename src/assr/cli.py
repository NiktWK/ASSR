import argparse
import json
from pathlib import Path
import time

from .engine import Engine
from .models import DecisionInput, ProjectSpec, State, TERMINAL, uid
from .settings import Settings


def main():
    p = argparse.ArgumentParser(prog="assr")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-demo")
    sub.add_parser("demo")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    export = sub.add_parser("export")
    export.add_argument("campaign_id")
    export.add_argument("--output", required=True)
    run = sub.add_parser("create")
    run.add_argument("spec")
    sub.add_parser("schemas")
    a = p.parse_args()
    s = Settings.env()
    if a.command in ("demo", "init-demo"):
        from . import demo_train
        from .demo import setup

        # setup accepts the source directory; package fixture is named demo_train.py.
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp)
            (src / "train.py").write_text(Path(demo_train.__file__).read_text())
            setup(s.root, src)
        if a.command == "init-demo":
            print("Demo profile prepared in " + str(s.root))
            return
    if a.command == "serve":
        if a.host not in ("127.0.0.1", "localhost", "::1") and not s.token:
            p.error("ASSR_TOKEN is required for non-loopback binding")
        import uvicorn
        from .api import create_app

        uvicorn.run(create_app(s), host=a.host, port=a.port, workers=1)
        return
    if a.command == "schemas":
        from . import models

        for name in (
            "ProjectSpec",
            "ExperimentSpec",
            "RunSpec",
            "ReviewDecision",
            "ResearchConclusion",
            "Envelope",
        ):
            print(json.dumps({"name": name, "schema": getattr(models, name).model_json_schema()}))
        return
    engine = Engine(s)
    if a.command == "create":
        import yaml

        spec = ProjectSpec.model_validate(yaml.safe_load(Path(a.spec).read_text()))
        print(json.dumps(engine.create(spec, uid()), indent=2))
        return
    if a.command == "export":
        Path(a.output).write_text(json.dumps(engine.store.get(a.campaign_id), ensure_ascii=False, indent=2))
        return
    if a.command == "demo":
        d = engine.create(
            ProjectSpec(name="Toy optimization", goal="Measure the effect of more optimization steps"), uid()
        )
        print("Campaign " + d["id"])
        for _ in range(100):
            d = engine.tick(d["id"])
            if d["state"] == State.AWAITING_APPROVAL:
                # Only this explicit demo command autoapproves the known fixture.
                d = engine.decision(
                    d["id"],
                    DecisionInput(revision=d["revision"], action="approve", comment="Explicit CLI demo"),
                    uid(),
                )
            if d["state"] in TERMINAL or d["state"] == State.PAUSED:
                break
            time.sleep(0.01)
        from .api import report

        out = s.root / (d["id"] + ".md")
        out.write_text(report(d))
        print(d["state"])
        print(out)
        if d["state"] != State.COMPLETED:
            raise SystemExit(d.get("last_error", "Demo did not finish"))


if __name__ == "__main__":
    main()
