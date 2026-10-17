"""Proposal drafting, critique, revision, and human approval."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from framework.models import Turn
with workflow.unsafe.imports_passed_through():
    from framework.webui import create_chat, claude_turn

WRITER_MODEL = "claude-sonnet-5-5"

@dataclass
class DraftInput:
    topic: str
    max_rounds: int = 2


CRITIC_MODEL = "claude-haiku-5-5"

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

    async def _critique(self) -> dict:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"},
                  "issues": {"type": "array", "items": {"type": "string"}}},
                  "required": ["ok", "issues"], "additionalProperties": False}
        response = await workflow.execute_activity(
            claude_turn,
            Turn(model=CRITIC_MODEL, agent="draft-reviewer",
                 webui_chat_id=self.writer_chat_id, output_schema=schema,
                 messages=[{"role": "user", "content":
                     "Review the latest proposal in this conversation:\n\n" + self.versions[-1]}]),
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        verdict = response["value"]
        if (not isinstance(verdict, dict) or type(verdict.get("ok")) is not bool
                or not isinstance(verdict.get("issues"), list)
                or not all(isinstance(i, str) for i in verdict["issues"])
                or (verdict["ok"] and verdict["issues"])):
            raise ApplicationError("Critic returned an invalid verdict", non_retryable=True)
        return {**verdict, "chat_id": response["chat_id"]}

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
            verdict = await self._critique()
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



