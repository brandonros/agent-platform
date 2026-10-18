"""
title: Organizational Claude Code
version: 0.5.1
description: Thin renderer for the official-SDK lab service.
"""
import asyncio
import hashlib
import json
import os

import httpx
from pydantic import BaseModel, Field
from typing import Literal


class OutputRenderer:
    """Translate SDK events into WebUI's supported structured response stream."""
    def __init__(self):
        self.items = []
        self.blocks = {}
        self.tools = {}

    def add(self, item):
        index = len(self.items)
        self.items.append(item)
        return {"type": "response.output_item.added", "output_index": index, "item": dict(item)}

    def render(self, event):
        kind = event.get("_type")
        if kind == "StreamEvent" and event.get("parent_tool_use_id"):
            return  # Delegated prose belongs to the Agent result, not the main answer.
        if kind == "StreamEvent":
            frame = event.get("event", {})
            index = frame.get("index", 0)
            if frame.get("type") == "content_block_start":
                block = frame.get("content_block", {})
                if block.get("type") in ("text", "thinking"):
                    reasoning = block["type"] == "thinking"
                    if reasoning and not frame.get("visible"):
                        return  # Do not create an empty panel for redacted thinking.
                    item = {"id": f"claude-output-{len(self.items)}", "type": "reasoning" if reasoning else "message", "status": "in_progress"}
                    if reasoning:
                        item["summary"] = [{"type": "summary_text", "text": ""}]
                    else:
                        item.update(role="assistant", content=[{"type": "output_text", "text": "", "annotations": []}])
                    self.blocks[index] = len(self.items)
                    yield self.add(item)
            elif frame.get("type") == "content_block_delta":
                delta = frame.get("delta", {})
                if delta.get("type") in ("text_delta", "thinking_delta"):
                    reasoning = delta["type"] == "thinking_delta"
                    text = delta.get("thinking" if reasoning else "text", "")
                    if not text:
                        return
                    if index not in self.blocks:
                        # Some stream providers omit the start frame.
                        yield from self.render({"_type": "StreamEvent", "event": {"type": "content_block_start", "index": index, "visible": True, "content_block": {"type": "thinking" if reasoning else "text"}}})
                    output_index = self.blocks[index]
                    item = self.items[output_index]
                    text = delta.get("thinking" if reasoning else "text", "")
                    item["summary" if reasoning else "content"][0]["text"] += text
                    yield {"type": "response.reasoning_summary_text.delta" if reasoning else "response.output_text.delta", "output_index": output_index, "item_id": item["id"], "content_index": 0, "summary_index": 0, "delta": text}
            elif frame.get("type") == "content_block_stop" and index in self.blocks:
                output_index = self.blocks.pop(index)
                self.items[output_index]["status"] = "completed"
                yield {"type": "response.output_item.done", "output_index": output_index, "item": self.items[output_index]}
        elif kind in ("AssistantMessage", "UserMessage"):
            for block in event.get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("_type") == "ToolUseBlock" and block["id"] not in self.tools:
                    self.tools[block["id"]] = len(self.items)
                    yield self.add({"type": "function_call", "id": block["id"], "call_id": block["id"], "name": block["name"], "arguments": json.dumps(block["input"]), "status": "in_progress"})
                elif block.get("_type") == "ToolResultBlock":
                    call_id = block["tool_use_id"]
                    if call_id in self.tools:
                        idx = self.tools[call_id]
                        self.items[idx]["status"] = "completed"
                        yield {"type": "response.output_item.done", "output_index": idx, "item": self.items[idx]}
                    content = block.get("content", "")
                    if isinstance(content, list) and all(isinstance(part, dict) and part.get("type") == "text" for part in content):
                        content = "\n\n".join(part.get("text", "") for part in content)
                    yield self.add({"type": "function_call_output", "id": call_id + "-result", "call_id": call_id,
                                    "output": [{"type": "input_text", "text": content if isinstance(content, str) else json.dumps(content)}], "status": "failed" if block.get("is_error") else "completed"})


class Pipe:
    class Valves(BaseModel):
        SERVICE_URL: str = "http://claude-runtime-server.chat.svc.cluster.local:8000"
        AGENT: str = "general"
        MODEL: str = "claude-haiku-5-5"

    class UserValves(BaseModel):
        EFFORT: Literal["Model default", "Low", "Medium", "High", "Extra high", "Max"] = Field(
            default="Model default", description="Claude effort. An explicit chat Reasoning Effort parameter takes precedence.")

    def __init__(self):
        self.valves = self.Valves()

    async def pipe(self, body: dict, __user__: dict, __metadata__: dict = None,
                   __task__: str = None, __event_emitter__=None):
        if __task__:
            yield "Claude Code"
            return
        metadata = __metadata__ or {}
        prompts = [m["content"] for m in body["messages"] if m["role"] == "user"]
        if not prompts or not all(isinstance(p, str) for p in prompts):
            yield "Execution rejected: this lab supports text messages only."
            return
        systems = [m["content"] for m in body["messages"] if m["role"] == "system" and m.get("content")]
        if not all(isinstance(s, str) for s in systems):
            yield "Execution rejected: system instructions must be text."
            return
        chat = metadata.get("chat_id")
        if not chat:
            yield "A chat ID is required. Open a saved or temporary WebUI chat before sending."
            return
        session_key = f"webui:{__user__['id']}:{chat}"
        if self.valves.AGENT != "general":
            session_key += ":" + self.valves.AGENT
        turn_key = metadata.get("message_id")
        parent_key = None
        # Saved WebUI history is a tree. Use response IDs, not text, to identify
        # the selected branch (including regenerate with identical prompts).
        if metadata.get("user_message_id") and not chat.startswith("local:"):
            from open_webui.models.chats import Chats
            saved = await Chats.get_chat_by_id(chat)
            if not saved or saved.user_id != __user__["id"]:
                yield "Chat is unavailable to this user."
                return
            user_message = saved.chat.get("history", {}).get("messages", {}).get(metadata["user_message_id"], {})
            parent_key = user_message.get("parentId")
        params = body.get("params") or {}
        selected_agent = body.get("runtime_agent", params.get("runtime_agent")) or self.valves.AGENT
        schema = body.get("output_schema", params.get("output_schema"))
        payload = {"agent": selected_agent, "prompt": prompts[-1], "session_key": session_key,
                   "previous_prompts": prompts[:-1], "model": self.valves.MODEL,
                   "turn_key": turn_key, "parent_turn_key": parent_key,
                   "system_prompt": "\n\n".join(systems)}
        if schema is not None:
            payload["output_schema"] = schema
        user_valves = __user__.get("valves") or self.UserValves()
        preference = user_valves.get("EFFORT", "Model default") if isinstance(user_valves, dict) else user_valves.EFFORT
        effort = body.get("reasoning_effort", (body.get("params") or {}).get("reasoning_effort", preference))
        effort = {"Model default": None, "Low": "low", "Medium": "medium", "High": "high", "Extra high": "xhigh", "Max": "max", "default": None, "auto": None, "": None}.get(effort, effort)
        if effort not in (None, "low", "medium", "high", "xhigh", "max"):
            yield "Execution rejected: unsupported effort level. Choose Model default, Low, Medium, High, Extra high, or Max."
            return
        if effort is not None:
            payload["effort"] = effort
        request_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        headers = {"Authorization": "Bearer " + os.environ["CLAUDE_SERVICE_API_KEY"],
                   "X-Owner-ID": __user__["id"], "Idempotency-Key": request_key}
        renderer = OutputRenderer()
        run = None
        finished = False
        async with httpx.AsyncClient(base_url=self.valves.SERVICE_URL, headers=headers,
                                     timeout=httpx.Timeout(30, read=240)) as client:
            try:
                for attempt in range(30):
                    try:
                        response = await client.post("/runs", json=payload)
                        response.raise_for_status()
                        run = response.json()
                        break
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code not in (502, 503, 504) or attempt == 29:
                            try:
                                detail = error.response.json().get("detail", "Request failed")
                            except ValueError:
                                detail = f"Service returned HTTP {error.response.status_code}"
                            raise RuntimeError(str(detail)) from error
                    except httpx.TransportError:
                        if attempt == 29:
                            raise
                    await asyncio.sleep(3)
                async def events():
                    cursor = 0
                    attempts = 0
                    while True:
                        try:
                            async with client.stream("GET", f"/runs/{run['id']}/events", params={"after": cursor}) as response:
                                if response.status_code in (502, 503, 504):
                                    raise httpx.ReadError("API is restarting")
                                response.raise_for_status()
                                event_id = cursor
                                async for line in response.aiter_lines():
                                    if line.startswith("id: "):
                                        event_id = int(line[4:])
                                    elif line.startswith("data: "):
                                        event = json.loads(line[6:])
                                        yield event
                                        cursor = event_id
                                        attempts = 0
                                        if event.get("_type") == "RunFinished":
                                            return
                            raise httpx.ReadError("Event stream closed before completion")
                        except (httpx.TransportError, httpx.TimeoutException):
                            attempts += 1
                            if attempts > 30:
                                raise
                            await asyncio.sleep(min(attempts, 3))
                async for event in events():
                    kind = event.get("_type")
                    if kind in ("RunQueued", "RunStarted") and __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {"description": "Waiting for an execution slot" if kind == "RunQueued" else "Claude is starting", "done": False}})
                    frame = event.get("event", {})
                    if kind == "StreamEvent" and frame.get("type") == "content_block_start" and frame.get("content_block", {}).get("type") == "thinking" and __event_emitter__:
                        await __event_emitter__({"type": "status", "data": {"description": "Thinking", "done": False}})
                    if schema is None:
                        for output_event in renderer.render(event):
                            yield output_event
                    if kind == "RunFinished":
                        finished = True
                        if schema is not None and event["status"] == "succeeded":
                            final = await client.get(f"/runs/{run['id']}")
                            final.raise_for_status()
                            yield json.dumps(final.json()["structured_output"])
                        if event["status"] != "succeeded":
                            yield f"\n\nExecution {event['status']}: {event.get('error') or 'stopped'}"
                        if __event_emitter__:
                            await __event_emitter__({"type": "status", "data": {
                                "description": "Complete" if event["status"] == "succeeded" else event["status"].capitalize(), "done": True}})
            except Exception as error:
                yield f"\n\nExecution failed: {str(error)}"
            except asyncio.CancelledError:
                # WebUI explicitly cancels the task on Stop. Ordinary upstream
                # transport failures reconnect above, rather than killing work.
                if run and not finished:
                    async def stop():
                        async with httpx.AsyncClient(base_url=self.valves.SERVICE_URL, headers=headers, timeout=15) as stop_client:
                            await stop_client.post(f"/runs/{run['id']}/cancel")
                    await asyncio.shield(asyncio.create_task(stop()))
                raise
