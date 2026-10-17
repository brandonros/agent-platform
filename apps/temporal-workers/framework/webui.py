"""Shared WebUI transport and retry-safe Temporal activities."""
import asyncio
import json
import os
import time
import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError
from framework.models import Turn

async def _heartbeat(seconds: float) -> None:
    # Heartbeat while waiting for the first token too, not only per chunk.
    while True:
        activity.heartbeat()
        await asyncio.sleep(seconds)


_auth_tokens = {}
_auth_locks = {}


def webui_credentials():
    return os.environ["WEBUI_EMAIL"], os.environ["WEBUI_PASSWORD"]


async def authorize(http, rejected_token=None):
    credentials = webui_credentials()
    lock = _auth_locks.setdefault(credentials, asyncio.Lock())
    async with lock:
        cached = _auth_tokens.get(credentials)
        if cached and cached[1] > time.monotonic() and cached[0] != rejected_token:
            http.headers["Authorization"] = cached[0]
            return
        for attempt in range(4):
            response = await http.post('/api/v1/auths/signin', json={
                'email': credentials[0], 'password': credentials[1]})
            if response.status_code != 429 or attempt == 3:
                break
            await asyncio.sleep(min(60, max(1, float(response.headers.get('Retry-After', 15)))))
        response.raise_for_status()
        token = 'Bearer ' + response.json()['token']
        _auth_tokens[credentials] = (token, time.monotonic() + 300)
        http.headers['Authorization'] = token


class WebUI:
    def __init__(self, http):
        self.http = http

    async def call(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        if response.status_code == 401:
            await authorize(self.http, rejected_token=self.http.headers.get('Authorization'))
            response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def chat(self, key):
        title = "Temporal " + key
        # Recover creation whose HTTP response was lost. A unique title is a
        # prototype correlation key; WebUI /new has no idempotency-key support.
        found = await self.call("GET", "/api/v1/chats/search", params={"text": title})
        exact = [c for c in found if c["title"] == title]
        if len(exact) > 1:
            raise ApplicationError("Ambiguous WebUI chat creation", non_retryable=True)
        if exact:
            return exact[0]["id"]
        created = await self.call("POST", "/api/v1/chats/new", json={"chat": {
            "title": title, "models": ["org_claude_code"],
            "history": {"messages": {}, "currentId": None}, "messages": []}})
        return created["id"]

    async def turn(self, cid, aid, prompt, model, agent, schema, effort):
        from uuid import UUID, uuid5
        uid = str(uuid5(UUID(aid), "user"))
        saved = (await self.call("GET", f"/api/v1/chats/{cid}"))["chat"]
        history = saved["history"]
        user = history["messages"].get(uid)
        parent = user.get("parentId") if user else history.get("currentId")
        model_id = {"claude-sonnet-5-5": "org_claude_sonnet", "claude-haiku-5-5": "org_claude_code"}[model]
        body = {"model": model_id, "stream": True, "chat_id": cid, "id": aid,
            "session_id": aid, "messages": [], "params": {
                "runtime_agent": agent or "general", "output_schema": schema,
                "reasoning_effort": effort},
            "user_message": {"id": uid, "role": "user", "content": prompt,
                "parentId": parent, "childrenIds": [], "done": True},
            "background_tasks": {"follow_up_generation": False}}
        while True:
            saved = (await self.call("GET", f"/api/v1/chats/{cid}"))["chat"]
            reply = saved["history"]["messages"].get(aid, {})
            if reply.get("done"):
                text = "".join(p.get("text", "") for i in reply.get("output", [])
                    if i.get("type") == "message" for p in i.get("content", [])) or reply.get("content", "")
                if reply.get("error") or any(f"Execution {status}:" in text
                        for status in ("failed", "rejected", "cancelled", "timed_out")):
                    raise ApplicationError(text, non_retryable=True)
                if not text.strip():
                    raise ApplicationError("WebUI returned an empty answer", non_retryable=True)
                return json.loads(text) if schema is not None else text
            tasks = await self.call("GET", f"/api/tasks/chat/{cid}")
            if not tasks.get("task_ids"):
                await self.call("POST", "/api/chat/completions", json=body)
            await asyncio.sleep(2)


@activity.defn
async def create_chat(key: str) -> str:
    async with httpx.AsyncClient(base_url=os.environ["WEBUI_URL"], timeout=30) as http:
        await authorize(http)
        return await WebUI(http).chat(key)


@activity.defn
async def claude_turn(turn: Turn) -> str | dict:
    from uuid import NAMESPACE_URL, uuid5
    info = activity.info()
    key = f"{info.workflow_run_id}:{turn.session_id or info.activity_id}"
    aid = str(uuid5(NAMESPACE_URL, f"{info.workflow_run_id}:{info.activity_id}"))
    beat = asyncio.create_task(_heartbeat(10))
    try:
        async with httpx.AsyncClient(base_url=os.environ["WEBUI_URL"], timeout=30) as http:
            await authorize(http)
            ui = WebUI(http)
            cid = turn.webui_chat_id or await ui.chat(key)
            activity.heartbeat({"chat_id": cid, "message_id": aid})
            value = await ui.turn(cid, aid, turn.messages[-1]["content"], turn.model,
                                  turn.agent, turn.output_schema, turn.effort)
            saved = (await ui.call("GET", f"/api/v1/chats/{cid}"))["chat"]
            output = saved["history"]["messages"][aid].get("output", [])
            return {"value": value, "chat_id": cid, "message_id": aid,
                    "evidence": [i for i in output if i.get("type") in
                                 ("function_call", "function_call_output")]}
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)


