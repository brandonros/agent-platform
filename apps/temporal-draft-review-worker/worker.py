"""Draft → critique → revise → human review, through saved Open WebUI conversations.

The workflow owns states, limits, and the human gate; each Claude call is an
activity that starts or reconnects to a saved WebUI response.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import httpx

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "draft-review-webui")
WRITER_MODEL = "claude-sonnet-5-5"
CRITIC_MODEL = "claude-haiku-5-5"


@dataclass
class DraftInput:
    topic: str
    max_rounds: int = 2


@dataclass
class Turn:
    # Workflow history remains the source for prompts; native tool context lives
    # in the service's persisted Claude session.
    messages: list[dict]
    model: str
    session_id: str = ""
    agent: str = ""  # Empty preserves the request identity of historical activities.
    output_schema: dict | None = None
    effort: str | None = None
    webui_chat_id: str | None = None


# ---------------------------------------------------------------- activities


async def _heartbeat(seconds: float) -> None:
    # Heartbeat while waiting for the first token too, not only per chunk.
    while True:
        activity.heartbeat()
        await asyncio.sleep(seconds)


class WebUI:
    def __init__(self, http):
        self.http = http

    async def call(self, method, path, **kwargs):
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
        login = await http.post("/api/v1/auths/signin", json={
            "email": os.environ["WEBUI_EMAIL"], "password": os.environ["WEBUI_PASSWORD"]})
        login.raise_for_status()
        http.headers["Authorization"] = "Bearer " + login.json()["token"]
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
            login = await http.post("/api/v1/auths/signin", json={
                "email": os.environ["WEBUI_EMAIL"], "password": os.environ["WEBUI_PASSWORD"]})
            login.raise_for_status()
            http.headers["Authorization"] = "Bearer " + login.json()["token"]
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


@activity.defn
async def critique(draft: str | dict) -> dict:
    """Review in the workflow chat; accept old activity inputs for replay."""
    cid = draft.get("chat_id") if isinstance(draft, dict) else None
    proposal = draft["draft"] if isinstance(draft, dict) else draft
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"},
              "issues": {"type": "array", "items": {"type": "string"}}},
              "required": ["ok", "issues"], "additionalProperties": False}
    response = await claude_turn(Turn(model=CRITIC_MODEL, agent="draft-reviewer",
        webui_chat_id=cid, output_schema=schema,
        messages=[{"role": "user", "content": "Review the latest proposal in this conversation:\n\n" + proposal}]))
    verdict = response["value"]
    if (not isinstance(verdict, dict) or type(verdict.get("ok")) is not bool
            or not isinstance(verdict.get("issues"), list)
            or not all(isinstance(i, str) for i in verdict["issues"])
            or (verdict["ok"] and verdict["issues"])):
        raise ApplicationError("Critic returned an invalid verdict", non_retryable=True)
    return {**verdict, "chat_id": response["chat_id"]}


# ------------------------------------------------------------------ workflow


@workflow.defn
class DraftReviewWebUI:
    def __init__(self) -> None:
        self.writer_chat_id = None
        self.state = "queued"
        self.versions: list[str] = []
        self.critiques: list[dict] = []
        self.review: tuple[str, str] | None = None
        self.notes: list[str] = []
        # The writer's conversation; durable because it is workflow state.
        self.chat: list[dict] = []

    @workflow.query
    def status(self) -> dict:
        return {
            "state": self.state,
            "chat_id": self.writer_chat_id,
            "writer_chat_id": self.writer_chat_id,
            "reviewer_chat_ids": [c["chat_id"] for c in self.critiques],
            "versions": len(self.versions),
            "critiques": self.critiques,
            "notes": self.notes,
            "latest": self.versions[-1] if self.versions else None,
        }

    @workflow.signal
    def submit_review(self, decision: str, note: str = "") -> None:
        if decision in ("approve", "revise", "reject"):
            self.review = (decision, note)

    async def _write(self, prompt: str) -> None:
        messages = self.chat + [{"role": "user", "content": prompt}]
        reply = await workflow.execute_activity(
            claude_turn,
            Turn(messages=messages, model=WRITER_MODEL, session_id=workflow.info().workflow_id,
                 agent="writer", webui_chat_id=self.writer_chat_id),
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=60),
            # Safe to reconnect: the service deduplicates the activity request key.
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        self.writer_chat_id = reply["chat_id"]
        reply = reply["value"]
        self.chat = messages + [{"role": "assistant", "content": reply}]
        self.versions.append(reply)

    @workflow.run
    async def run(self, inp: DraftInput) -> dict:
        if not inp.topic.strip() or not 0 <= inp.max_rounds <= 5:
            raise ApplicationError("Invalid input", non_retryable=True)

        # Keep already-running experiment histories replayable. New workflows
        # durably record their one chat before any model work begins.
        shared_chat = workflow.patched("one-webui-chat-per-workflow")
        if shared_chat:
            self.state = "creating_chat"
            self.writer_chat_id = await workflow.execute_activity(
                create_chat, workflow.info().run_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )

        self.state = "drafting"
        await self._write(
            f"Write a one-page engineering proposal on: {inp.topic}\n"
            "Include a recommendation, costs, risks, and a first step. Use Markdown."
        )

        # Automatic critique/revise rounds; the limit is enforced here, not in a prompt.
        for _ in range(inp.max_rounds):
            self.state = "critiquing"
            verdict = await workflow.execute_activity(
                critique,
                {"draft": self.versions[-1], "chat_id": self.writer_chat_id}
                if shared_chat else self.versions[-1],
                start_to_close_timeout=timedelta(minutes=10),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            self.critiques.append(verdict)
            if verdict["ok"]:
                break
            self.state = "revising"
            issues = "\n".join(f"- {i}" for i in verdict["issues"])
            await self._write(f"A reviewer found these issues:\n{issues}\nRevise the full proposal.")

        # Human gate: wait durably for a decision; revisions loop back here.
        while True:
            self.state = "waiting_for_review"
            if not await self._await_review():
                self.state = "expired"
                return self.status()
            # Consume the decision so the next revision waits for a new one.
            (decision, note), self.review = self.review, None
            if decision == "approve":
                self.state = "approved"
                return self.status()
            if decision == "reject":
                self.state = "rejected"
                self.notes.append(note)
                return self.status()
            self.notes.append(note)
            self.state = "revising"
            await self._write(f"The human reviewer asks: {note}\nRevise the full proposal.")

    async def _await_review(self) -> bool:
        try:
            await workflow.wait_condition(lambda: self.review is not None, timeout=timedelta(days=3))
            return True
        except asyncio.TimeoutError:
            return False



def successful_tool(reply, name, argument, expected):
    """Use persisted tool events, never the model's assertion that it ran a tool."""
    items = reply.get("evidence", [])
    for call in items:
        if call.get("type") != "function_call" or call.get("name") != name:
            continue
        args = call.get("arguments", {})
        if isinstance(args, str):
            args = json.loads(args)
        if args.get(argument) != expected:
            continue
        for result in items:
            if (result.get("type") == "function_call_output"
                    and result.get("call_id") == call.get("call_id")
                    and result.get("status") == "completed"):
                return result
    return None


@workflow.defn
class AgentCapabilities:
    """Maintained end-to-end example: a named agent, skill, MCP, and code routing."""
    def __init__(self):
        self.state = "queued"
        self.chat_id = None
        self.result = None
        self.checks = {}

    @workflow.query
    def status(self):
        return {"state": self.state, "chat_id": self.chat_id,
                "checks": self.checks, "result": self.result}

    async def turn(self, prompt, schema=None):
        return await workflow.execute_activity(
            claude_turn, Turn(messages=[{"role": "user", "content": prompt}],
                model=WRITER_MODEL, agent="writer", webui_chat_id=self.chat_id,
                output_schema=schema),
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3))

    @workflow.run
    async def run(self):
        self.state = "creating_chat"
        self.chat_id = await workflow.execute_activity(
            create_chat, workflow.info().run_id,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3))
        nonce = "capabilities-" + str(workflow.uuid4())
        self.state = "using_skill"
        skill = await self.turn("Use the team-glossary Skill to define ACE. Keep the answer brief.")
        self.checks["skill"] = successful_tool(skill, "Skill", "skill", "team-glossary") is not None
        self.state = "using_mcp"
        mcp = await self.turn(f"Call mcp__lab__echo with nonce {nonce}. Report the actual result.")
        echoed = successful_tool(mcp, "mcp__lab__echo", "nonce", nonce)
        self.checks["mcp"] = echoed is not None and nonce in json.dumps(echoed.get("output"))
        self.state = "structured_response"
        schema = {"type": "object", "properties": {
            "ace": {"type": "string"}, "nonce": {"type": "string"},
            "decision": {"type": "string", "enum": ["ready", "needs_review"]}},
            "required": ["ace", "nonce", "decision"], "additionalProperties": False}
        response = await self.turn(
            "Using our conversation, return ACE's expanded name and the exact nonce returned by MCP. "
            "Set decision to ready only if both tasks succeeded, otherwise needs_review.", schema)
        self.result = response["value"]
        self.checks["values"] = (isinstance(self.result, dict)
            and self.result.get("ace") == "Agent Command Environment"
            and self.result.get("nonce") == nonce)
        # The model proposes a decision; code gates it on observed tool evidence.
        self.state = ("completed" if all(self.checks.values())
            and self.result.get("decision") == "ready" else "needs_review")
        return self.status()


# -------------------------------------------------------------------- worker


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "temporal-dev-server.chat.svc.cluster.local:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    logging.info("worker up: queue=%s webui=%s", TASK_QUEUE, os.environ["WEBUI_URL"])
    await Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[DraftReviewWebUI, AgentCapabilities],
        activities=[create_chat, claude_turn, critique],
        # Service capacity and per-session serialization are enforced centrally.
        max_concurrent_activities=int(os.getenv("MAX_CONCURRENT_ACTIVITIES", "4")),
    ).run()


if __name__ == "__main__":
    asyncio.run(main())
