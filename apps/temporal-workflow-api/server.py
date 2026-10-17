"""Small HTTP client of Temporal. No workflow execution or local persistence."""
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from temporalio.client import Client, WorkflowFailureError, WorkflowQueryFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Start(Input):
    type: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    task_queue: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    args: list = Field(default_factory=list)


class Signal(Input):
    name: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    args: list = Field(default_factory=list)


async def authorize(authorization: str = Header(default="")):
    if not hmac.compare_digest(authorization, "Bearer " + os.environ["WORKFLOW_API_TOKEN"]):
        raise HTTPException(401, "Invalid bearer token")


@asynccontextmanager
async def lifespan(app):
    if not os.environ.get("WORKFLOW_API_TOKEN"):
        raise RuntimeError("WORKFLOW_API_TOKEN is required")
    app.state.temporal = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"], namespace=os.getenv("TEMPORAL_NAMESPACE", "default"))
    yield


app = FastAPI(lifespan=lifespan, dependencies=[Depends(authorize)])
TIMEOUT = timedelta(seconds=10)


@app.exception_handler(RPCError)
async def rpc_error(request, exc):
    from fastapi.responses import JSONResponse
    code = 404 if exc.status == RPCStatusCode.NOT_FOUND else 503
    return JSONResponse(status_code=code, content={"detail": "Workflow not found" if code == 404 else "Temporal unavailable"})


async def owned_handle(wid):
    handle = app.state.temporal.get_workflow_handle(wid)
    desc = await handle.describe(rpc_timeout=TIMEOUT)
    memo = await desc.memo()
    if memo.get("api") != "temporal-workflow-api":
        raise HTTPException(404, "Workflow not found")
    return app.state.temporal.get_workflow_handle(wid, run_id=desc.run_id), desc


@app.post("/workflows", status_code=202)
async def start(body: Start, response: Response,
                idempotency_key: str | None = Header(default=None, max_length=200)):
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    wid = "api-" + (hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else str(uuid4()))
    try:
        await app.state.temporal.start_workflow(
            body.type, args=body.args,
            id=wid, task_queue=body.task_queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            memo={"api": "temporal-workflow-api", "fingerprint": fingerprint},
            rpc_timeout=TIMEOUT)
    except WorkflowAlreadyStartedError:
        _, desc = await owned_handle(wid)
        if (await desc.memo()).get("fingerprint") != fingerprint:
            raise HTTPException(409, "Idempotency key already used with different input")
    response.headers["Location"] = f"/workflows/{wid}"
    return {"id": wid, "url": f"/workflows/{wid}"}


@app.get("/workflows/{wid}")
async def get(wid: str):
    handle, desc = await owned_handle(wid)
    status = desc.status.name.lower()
    result = {"id": wid, "status": status}
    if status == "running":
        try:
            result["progress"] = await handle.query("status", rpc_timeout=TIMEOUT)
        except (RPCError, WorkflowQueryFailedError):
            # Status remains available when no worker can answer the live query.
            result["progress"] = None
    elif status == "completed":
        result["result"] = await handle.result(follow_runs=False, rpc_timeout=TIMEOUT)
    elif status in ("failed", "canceled", "terminated", "timed_out"):
        try:
            await handle.result(follow_runs=False, rpc_timeout=TIMEOUT)
        except WorkflowFailureError as exc:
            result["error"] = str(exc.cause or exc)
    return result


@app.post("/workflows/{wid}/signals", status_code=202)
async def signal(wid: str, body: Signal):
    handle, desc = await owned_handle(wid)
    if desc.status.name != "RUNNING":
        raise HTTPException(409, "Workflow is no longer running")
    await handle.signal(body.name, args=body.args, rpc_timeout=TIMEOUT)
    return {"id": wid, "signal": body.name, "accepted": True}
