from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import threading

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .engine import Engine
from .models import DecisionInput, ProjectSpec
from .settings import Settings
from .store import Conflict


def report(d):
    lines = [
        f"# {d['project']['name']}",
        "",
        f"Mode: {d['project']['mode']} | State: {d['state']}",
        "",
        d["project"]["goal"],
        "",
        "## Protocol",
        json.dumps(d.get("experiment", {}), ensure_ascii=False, indent=2),
        "",
        "## Measured runs",
        "| Run | Variant | Seed | Status | Accuracy | Commit |",
        "|---|---|---|---|---|---|",
    ]
    for r in d["runs"]:
        s = r["spec"]
        metric = r.get("result", {}).get("metrics", {}).get("accuracy", "—")
        lines.append(
            f"| {s['run_id']} | {r['variant']} / {r['phase']} | {s['seed']} | {r['status']} | {metric} | {s['commit_sha']} |"
        )
    for name in ["summary", "conclusion", "reviews"]:
        lines.extend(
            ["", "## " + name, "```json", json.dumps(d.get(name, {}), ensure_ascii=False, indent=2), "```"]
        )
    lines += [
        "",
        "## Resources",
        f"GPU seconds reserved: {d['gpu_reserved_seconds']}",
        f"LLM USD reserved: {d['llm_reserved_usd']}",
        "Reservations are conservative ceilings, not measured billing.",
        "",
        "## Reproduction",
        f"assr export {d['id']} --output campaign.json",
        "Use RunSpec with the same administrator profile and source/image/data/evaluator digests.",
        "Demo measurements are CPU fixtures, not GPU acceptance or scientific validation.",
    ]
    return "\n".join(lines)


def create_app(settings=None, engine=None):
    settings = settings or Settings.env()
    engine = engine or Engine(settings)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            for d in engine.store.list():
                if stop.is_set():
                    break
                try:
                    engine.tick(d["id"])
                except Exception:
                    import logging

                    logging.getLogger("assr").exception("Worker transition failed")
            stop.wait(0.5)

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=worker, daemon=True)
        if settings.worker:
            thread.start()
        yield
        stop.set()
        if settings.worker:
            thread.join(timeout=2)

    app = FastAPI(title="ASSR", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    def auth(authorization: str = Header(default="")):
        if settings.token and not secrets.compare_digest(authorization, "Bearer " + settings.token):
            raise HTTPException(401, "Authentication required")

    def key(idempotency_key: str = Header(min_length=8, max_length=100)):
        return idempotency_key

    def get(cid):
        try:
            return engine.store.get(cid)
        except KeyError:
            raise HTTPException(404, "Campaign not found")

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.middleware("http")
    async def security(request, call_next):
        # Reject browser cross-site mutations even in the unauthenticated localhost demo.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=403, content={"detail": "Cross-origin writes rejected"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/campaigns", dependencies=[Depends(auth)])
    def campaigns():
        return [engine.public(x) for x in engine.store.list()]

    @app.post("/campaigns", dependencies=[Depends(auth)])
    def create(spec: ProjectSpec, k=Depends(key)):
        return engine.create(spec, k)

    @app.get("/campaigns/{cid}", dependencies=[Depends(auth)])
    def campaign(cid: str):
        return engine.public(get(cid))

    @app.get("/campaigns/{cid}/events", dependencies=[Depends(auth)])
    def events(cid: str):
        get(cid)
        return engine.store.history(cid)

    @app.post("/campaigns/{cid}/decisions", dependencies=[Depends(auth)])
    def decide(cid: str, body: DecisionInput, k=Depends(key)):
        get(cid)
        return engine.decision(cid, body, k)

    for action in ("pause", "resume", "cancel"):

        def factory(action):
            def handler(cid: str, body: DecisionInput, k=Depends(key)):
                if body.action != action:
                    raise ValueError("action does not match endpoint")
                get(cid)
                return engine.decision(cid, body, k)

            return handler

        app.post("/campaigns/{cid}/" + action, dependencies=[Depends(auth)])(factory(action))

    @app.get("/campaigns/{cid}/report", response_class=PlainTextResponse, dependencies=[Depends(auth)])
    def get_report(cid: str):
        return report(get(cid))

    @app.get("/runs/{rid}", dependencies=[Depends(auth)])
    def run(rid: str):
        for d in engine.store.list():
            for r in d["runs"]:
                if r["spec"]["run_id"] == rid:
                    return r
        raise HTTPException(404, "Run not found")

    @app.get("/experiments/{eid}", dependencies=[Depends(auth)])
    def experiment(eid: str):
        for d in engine.store.list():
            for item in d.get("experiments", []):
                if item["experiment_id"] == eid:
                    return item
        raise HTTPException(404, "Experiment not found")

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
