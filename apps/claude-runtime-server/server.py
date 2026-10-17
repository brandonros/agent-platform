"""Single API replica coordinating durable sessions and bounded Kubernetes Jobs.

The bearer credential authenticates trusted adapters. X-Owner-ID is asserted by
those adapters, never accepted directly from an untrusted browser.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal
from jsonschema import Draft7Validator, SchemaError
from jobs import KubernetesJobs

def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


LIVE = ("queued", "running", "cancelling")

class Turn(BaseModel):
    agent: str = "general"
    output_schema: dict | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    prompt: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None
    model: str = "claude-haiku-5-5"
    session_key: str | None = Field(default=None, max_length=300)
    previous_prompts: list[str] | None = None
    system_prompt: str = Field(default="", max_length=100_000)
    turn_key: str | None = Field(default=None, max_length=300)
    parent_turn_key: str | None = Field(default=None, max_length=300)

class Store:
    def __init__(self, path):
        # Operations run synchronously on one ASGI event loop; TestClient starts
        # that loop on a different thread than the application factory.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, sdk_id TEXT);
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE, request TEXT,
                session_id TEXT, status TEXT, result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, payload TEXT);
            CREATE INDEX IF NOT EXISTS event_run ON events(run_id, id);
        """)
        for table, columns in {
            "sessions": {"owner": "TEXT NOT NULL DEFAULT 'service'"},
            "runs": {"structured_output": "TEXT", "event_offset": "INTEGER NOT NULL DEFAULT 0", "parent_id": "TEXT", "turn_key": "TEXT", "sdk_id": "TEXT", "checkpoint": "TEXT", "cancel_at": "REAL"},
        }.items():
            present = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, declaration in columns.items():
                if name not in present:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        if "caller_key" not in {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}:
            self.db.execute("ALTER TABLE sessions ADD COLUMN caller_key TEXT")
            self.db.execute("CREATE UNIQUE INDEX session_caller ON sessions(caller_key)")
            self.db.commit()

        # Existing WebUI mappings have an authenticated user's ID embedded by the pipe.
        for row in self.db.execute("SELECT id,caller_key FROM sessions WHERE owner='service'").fetchall():
            if row["caller_key"] and row["caller_key"].startswith("webui:"):
                self.db.execute("UPDATE sessions SET owner=? WHERE id=?", (row["caller_key"].split(":", 2)[1], row["id"]))
        for row in self.db.execute("SELECT r.id,r.request,r.request_key,s.owner FROM runs r JOIN sessions s ON s.id=r.session_id").fetchall():
            request = json.loads(row["request"])
            if "turn_key" not in request or "agent" not in request or "output_schema" not in request or "effort" not in request:
                normalized = Turn(**request).model_dump_json()
                scoped = hashlib.sha256(f"{row['owner']}:{row['request_key']}".encode()).hexdigest() if "turn_key" not in request else row["request_key"]
                self.db.execute("UPDATE runs SET request=?,request_key=? WHERE id=?", (normalized, scoped, row["id"]))
                for event in self.db.execute("SELECT payload FROM events WHERE run_id=? ORDER BY id", (row["id"],)).fetchall():
                    payload = json.loads(event[0])
                    sdk_id = (payload.get("session_id") or (payload.get("data") or {}).get("session_id")) if payload.get("_type") in ("SystemMessage", "AssistantMessage", "ResultMessage") else None
                    if sdk_id:
                        self.db.execute("UPDATE runs SET sdk_id=? WHERE id=?", (sdk_id, row["id"]))
                    if payload.get("_type") in ("AssistantMessage", "UserMessage") and payload.get("uuid"):
                        self.db.execute("UPDATE runs SET checkpoint=? WHERE id=?", (payload["uuid"], row["id"]))
        self.db.commit()

    def event(self, run_id, payload):
        self.db.execute("INSERT INTO events(run_id,payload) VALUES (?,?)", (run_id, json.dumps(payload)))
        self.db.commit()

    def run(self, run_id):
        row = self.db.execute("SELECT id,session_id,status,result,error,structured_output FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown run")
        result = dict(row)
        result["structured_output"] = json.loads(result["structured_output"]) if result["structured_output"] else None
        return result

    def finish(self, run_id, status, result=None, error=None, structured_output=None):
        self.db.execute("UPDATE runs SET status=?,result=?,error=?,structured_output=? WHERE id=?", (status, result, error, json.dumps(structured_output) if structured_output is not None else None, run_id))
        self.event(run_id, {"_type": "RunFinished", "status": status, "error": error})


def create_app(data_dir=None, api_key=None, backend=None):
    root = Path(data_dir or os.getenv("DATA_DIR", "/data"))
    key = api_key or os.environ.get("API_KEY", "")
    if not key:
        raise RuntimeError("API_KEY is required")
    root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "runs.sqlite3")
    jobs = backend or KubernetesJobs()
    capacity = int(os.getenv("MAX_ACTIVE_RUNS", "2"))

    def session_path(sid):
        return root / "sessions" / sid

    def run_path(row):
        return session_path(row["session_id"]) / "runs" / row["id"]

    def migrate_session(sid):
        target = session_path(sid)
        target.mkdir(parents=True, exist_ok=True)
        legacy = root / "workspaces" / sid
        if legacy.exists() and not (target / "workspace").exists():
            shutil.copytree(legacy, target / "workspace", symlinks=True)
            encoded = "".join(c if c.isalnum() else "-" for c in str(legacy))
            transcript = root / "home/.claude/projects" / encoded
            if transcript.exists():
                destination = target / "home/.claude/projects" / encoded
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(transcript, destination)
        return target

    def ingest(row):
        path = run_path(row) / "events.jsonl"
        if not path.exists():
            return
        with path.open("rb") as events:
            events.seek(row["event_offset"])
            while line := events.readline():
                if not line.endswith(b"\n"):
                    break
                payload = json.loads(line)
                sdk_id = payload.get("session_id") or (payload.get("data") or {}).get("session_id")
                if sdk_id:
                    store.db.execute("UPDATE sessions SET sdk_id=? WHERE id=?", (sdk_id, row["session_id"]))
                    store.db.execute("UPDATE runs SET sdk_id=? WHERE id=?", (sdk_id, row["id"]))
                if payload.get("_type") in ("AssistantMessage", "UserMessage") and payload.get("uuid"):
                    store.db.execute("UPDATE runs SET checkpoint=? WHERE id=?", (payload["uuid"], row["id"]))
                store.db.execute("UPDATE runs SET event_offset=? WHERE id=?", (events.tell(), row["id"]))
                store.event(row["id"], payload)  # offset + event commit atomically

    async def reconcile():
        while True:
            try:
                rows = store.db.execute("SELECT * FROM runs WHERE status IN ('running','cancelling') ORDER BY rowid").fetchall()
                for row in rows:
                    ingest(row)
                    result_path = run_path(row) / "result.json"
                    if result_path.exists():
                        # The writer may have appended its final messages after
                        # our first EOF observation but before result.json.
                        ingest(store.db.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone())
                        result = json.loads(result_path.read_text())
                        store.finish(row["id"], **result)
                        continue
                    status = await jobs.status(row["id"])
                    if status in ("missing", "failed", "succeeded"):
                        store.finish(row["id"], "cancelled" if row["cancel_at"] else "interrupted", error="Worker stopped without a final result; not automatically retried")
                    elif row["cancel_at"] and time.time() - row["cancel_at"] > 15:
                        await jobs.cancel(row["id"])
                        # Keep session locked until Kubernetes confirms the pod is gone.
                rows = store.db.execute("SELECT * FROM runs WHERE status='queued' ORDER BY rowid").fetchall()
                busy = {r[0] for r in store.db.execute("SELECT session_id FROM runs WHERE status IN ('running','cancelling')")}
                for row in rows:
                    if len(busy) >= capacity:
                        break
                    if row["session_id"] in busy:
                        continue
                    parent = store.db.execute("SELECT * FROM runs WHERE id=?", (row["parent_id"],)).fetchone()
                    if parent and parent["status"] in LIVE:
                        continue
                    target = migrate_session(row["session_id"])
                    fork = bool(parent and parent["session_id"] != row["session_id"])
                    if fork:
                        source = migrate_session(parent["session_id"])
                        # Do not snapshot while another turn mutates the source workspace.
                        if parent["session_id"] in busy:
                            continue
                        for name in ("home", "workspace"):
                            if (source / name).exists():
                                shutil.copytree(source / name, target / name, symlinks=True, dirs_exist_ok=True)
                    sdk_id = parent["sdk_id"] if fork else store.db.execute("SELECT sdk_id FROM sessions WHERE id=?", (row["session_id"],)).fetchone()[0]
                    if fork and (not sdk_id or not parent["checkpoint"]):
                        store.finish(row["id"], "failed", error="Parent turn has no native fork checkpoint")
                        continue
                    spec = json.loads(row["request"])
                    profiles = json.loads((Path(os.getenv("AGENT_CONFIG_DIR", Path(__file__).parent)) / "profiles.json").read_text())
                    spec["profile"] = profiles[spec.get("agent", "general")]
                    spec.update(sdk_id=sdk_id, fork=fork, resume_at=parent["checkpoint"] if fork else None)
                    path = run_path(row)
                    path.mkdir(parents=True, exist_ok=True)
                    atomic_json(path / "request.json", spec)
                    # Commit intent before contacting Kubernetes. A crash before
                    # creation is an interrupted run, never a silent replay.
                    store.db.execute("UPDATE runs SET status='running' WHERE id=?", (row["id"],))
                    store.event(row["id"], {"_type": "RunStarted", "session_id": row["session_id"]})
                    await jobs.submit(row["id"], row["session_id"], target)
                    busy.add(row["session_id"])
                app.state.scheduler_error = None
            except Exception as error:
                app.state.scheduler_error = str(error)
                import logging
                logging.exception("Job reconciliation failed")
            await asyncio.sleep(.2)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(reconcile())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await jobs.close()
        store.db.close()

    app = FastAPI(title="Configured Claude Code", lifespan=lifespan)
    app.state.store = store
    app.state.scheduler_error = None

    async def auth(authorization: str = Header(default=""), x_owner_id: str = Header(default="service", max_length=300)):
        if not hmac.compare_digest(authorization, "Bearer " + key):
            raise HTTPException(401, "Bearer token required")
        return x_owner_id

    def owned_run(run_id, owner):
        row = store.db.execute("SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id WHERE r.id=? AND s.owner=?", (run_id, owner)).fetchone()
        if row is None:
            raise HTTPException(404, "Unknown run")
        return row

    @app.get("/health")
    async def health():
        return {"status": "degraded" if app.state.scheduler_error else "ok", "scheduler_error": app.state.scheduler_error}

    @app.post("/runs")
    async def start(turn: Turn, owner=Depends(auth), idempotency_key: str = Header(min_length=1, max_length=200)):
        profiles = json.loads((Path(os.getenv("AGENT_CONFIG_DIR", Path(__file__).parent)) / "profiles.json").read_text())
        if turn.agent not in profiles:
            raise HTTPException(422, "Unknown agent profile")
        if turn.output_schema is not None:
            try:
                Draft7Validator.check_schema(turn.output_schema)
            except SchemaError as error:
                raise HTTPException(422, "Invalid output schema: " + error.message)
        request = turn.model_dump_json()
        request_key = hashlib.sha256(f"{owner}:{idempotency_key}".encode()).hexdigest()
        previous = store.db.execute("SELECT id,request FROM runs WHERE request_key=?", (request_key,)).fetchone()
        if previous:
            if previous["request"] != request:
                raise HTTPException(409, "Idempotency key was used with a different request")
            return store.run(previous["id"])
        if turn.session_id and turn.session_key:
            raise HTTPException(422, "Use session_id or session_key, not both")
        sid = turn.session_id
        parent = None
        if sid:
            session = store.db.execute("SELECT * FROM sessions WHERE id=? AND owner=?", (sid, owner)).fetchone()
            if not session:
                raise HTTPException(404, "Unknown session")
            parent = store.db.execute("SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
        elif turn.session_key:
            history = store.db.execute("SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id WHERE s.owner=? AND (s.caller_key=? OR json_extract(r.request,'$.session_key')=?) ORDER BY r.rowid", (owner, turn.session_key, turn.session_key)).fetchall()
            if turn.parent_turn_key:
                parent = next((r for r in reversed(history) if r["turn_key"] == turn.parent_turn_key), None)
                if parent is None:
                    # WebUI copies response IDs when cloning a chat. Resolve only
                    # within this owner and force a native fork into the new chat.
                    matches = store.db.execute("SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id WHERE s.owner=? AND r.turn_key=?", (owner, turn.parent_turn_key)).fetchall()
                    if len(matches) == 1:
                        parent = matches[0]
                if parent is None:
                    # Upgrade compatibility: old runs did not record WebUI IDs.
                    for candidate in reversed(history):
                        prior = json.loads(candidate["request"])
                        if candidate["turn_key"] is None and (prior.get("previous_prompts") or []) + [prior["prompt"]] == turn.previous_prompts:
                            parent = candidate
                            break
                    if parent is None:
                        raise HTTPException(409, "Parent response is not mapped to a Claude run")
            elif turn.previous_prompts:
                # Compatibility for Temporal/API callers without response IDs.
                for candidate in reversed(history):
                    prior = json.loads(candidate["request"])
                    if (prior.get("previous_prompts") or []) + [prior["prompt"]] == turn.previous_prompts:
                        parent = candidate
                        break
                if parent is None:
                    raise HTTPException(409, "History has no matching Claude turn; supply parent_turn_key")
            if parent:
                latest = store.db.execute("SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1", (parent["session_id"],)).fetchone()[0]
                if latest == parent["id"] and json.loads(parent["request"]).get("session_key") == turn.session_key:
                    sid = parent["session_id"]
        if not sid:
            sid = str(uuid4())
            store.db.execute("INSERT INTO sessions(id,sdk_id,caller_key,owner) VALUES (?,NULL,NULL,?)", (sid, owner))
        if turn.turn_key:
            duplicate = store.db.execute("SELECT r.id FROM runs r JOIN sessions s ON s.id=r.session_id WHERE s.owner=? AND r.turn_key=? AND json_extract(r.request,'$.session_key')=?", (owner, turn.turn_key, turn.session_key)).fetchone()
            if duplicate:
                raise HTTPException(409, "Response ID already used with different input")
        rid = str(uuid4())
        store.db.execute("INSERT INTO runs(id,request_key,request,session_id,status,parent_id,turn_key) VALUES (?,?,?,?,?,?,?)", (rid, request_key, request, sid, "queued", parent["id"] if parent else None, turn.turn_key))
        store.event(rid, {"_type": "RunQueued", "session_id": sid})
        return store.run(rid)

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, owner=Depends(auth)):
        owned_run(run_id, owner)
        return store.run(run_id)

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, after: int = Query(default=0, ge=0), owner=Depends(auth)):
        owned_run(run_id, owner)
        async def stream():
            cursor = after
            while True:
                live = store.run(run_id)["status"] in LIVE
                rows = store.db.execute("SELECT id,payload FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, cursor)).fetchall()
                for row in rows:
                    cursor = row["id"]
                    yield f"id: {cursor}\ndata: {row['payload']}\n\n"
                if not live:
                    return
                if not rows:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(.2)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str, owner=Depends(auth)):
        row = owned_run(run_id, owner)
        if row["status"] == "queued":
            store.finish(run_id, "cancelled")
        elif row["status"] in LIVE:
            path = run_path(row)
            path.mkdir(parents=True, exist_ok=True)
            (path / "cancel").touch()
            store.db.execute("UPDATE runs SET status='cancelling',cancel_at=COALESCE(cancel_at,?) WHERE id=?", (time.time(), run_id))
            store.db.commit()
        return store.run(run_id)

    return app
