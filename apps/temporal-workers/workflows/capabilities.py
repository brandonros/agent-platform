"""Agent, skill, MCP and structured-result acceptance workflow."""
import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from framework.models import Turn
with workflow.unsafe.imports_passed_through():
    from framework.webui import create_chat, claude_turn

WRITER_MODEL = "claude-sonnet-5-5"

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


