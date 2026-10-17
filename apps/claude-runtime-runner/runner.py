"""One SDK turn in a session-scoped filesystem. No API or Kubernetes credentials."""
import asyncio
from dataclasses import fields, is_dataclass
import json
import os
from pathlib import Path
import sys
from jsonschema import Draft7Validator

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultDeny, ResultMessage


def encode(value):
    if is_dataclass(value):
        return {"_type": type(value).__name__, **{f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


async def deny_unapproved(name, args, context):
    return PermissionResultDeny(message=f"Tool {name} is not approved for this profile")


def read_boundary(workspace, config, extra_tools=(), structured=False):
    """Enforce read scope even for tools pre-approved by allowed_tools."""
    roots = (workspace.resolve(), config.resolve())
    async def hook(input, tool_use_id, context):
        name, args = input.get("tool_name"), input.get("tool_input", {})
        if name in ("Agent", "Task"):
            if args.get("subagent_type") in {p.stem for p in (config / ".claude/agents").glob("*.md")}:
                return {}
        if name == "StructuredOutput" and structured:
            return {}
        if name == "Skill" or name in extra_tools:
            return {}
        if name not in ("Read", "Glob", "Grep"):
            allowed = False
        else:
            raw = args.get("file_path") if name == "Read" else args.get("path", str(workspace))
            path = Path(raw or str(workspace)).expanduser()
            if not path.is_absolute():
                path = workspace / path
            path = path.resolve()
            allowed = any(path == root or root in path.parents for root in roots)
            if name == "Glob":
                pattern = Path(args.get("pattern", ""))
                allowed = allowed and not pattern.is_absolute() and ".." not in pattern.parts
        if allowed:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "Read access is limited to the session workspace and organizational configuration."}}
    return hook


async def execute(session, run_id, client_factory=ClaudeSDKClient):
    run = session / "runs" / run_id
    if (run / "cancel").exists():
        atomic_json(run / "result.json", {"status": "cancelled", "result": None, "error": None})
        return
    spec = json.loads((run / "request.json").read_text())
    workspace = session / "workspace"
    workspace.mkdir(exist_ok=True)
    (session / "home").mkdir(exist_ok=True)
    config = Path(os.getenv("AGENT_CONFIG_DIR", "/config"))
    for name in (".claude", "CLAUDE.md"):
        target = workspace / name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(config / name, target_is_directory=name == ".claude")
    profile = spec.get("profile", {})
    extra_tools = profile.get("allowed_tools", [])
    schema = spec.get("output_schema")
    if schema is not None:
        Draft7Validator.check_schema(schema)
    options = ClaudeAgentOptions(
        effort=spec.get("effort"),
        output_format={"type": "json_schema", "schema": schema} if schema is not None else None,
        extra_args={"agent": profile["native_agent"]} if profile.get("native_agent") else {},
        mcp_servers=profile.get("mcp_servers", {}), thinking=profile.get("thinking"),
        cwd=str(workspace), model=spec["model"], resume=spec.get("sdk_id"),
        fork_session=spec.get("fork", False), resume_session_at=spec.get("resume_at"),
        env={"HOME": str(session / "home"), "CLAUDE_CONFIG_DIR": str(session / "home/.claude"), "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1"},
        system_prompt={"type": "preset", "preset": "claude_code", "append": profile.get("prompt", "") + "\n\n" + spec["system_prompt"], "snapshot": False},
        setting_sources=["project"], tools=["Skill", "Read", "Glob", "Grep", "Agent"] + (["StructuredOutput"] if schema is not None else []),
        allowed_tools=["Skill", "Read", "Glob", "Grep", "Agent"] + extra_tools, permission_mode="default",
        hooks={"PreToolUse": [HookMatcher(hooks=[read_boundary(workspace, config, extra_tools, schema is not None)])]},
        can_use_tool=deny_unapproved, max_turns=8, max_budget_usd=1.0, include_partial_messages=True,
    )
    result = {"status": "failed", "error": "SDK ended without a ResultMessage", "result": None}
    async def watch_cancel(client):
        while not (run / "cancel").exists():
            await asyncio.sleep(.2)
        await client.interrupt()
    try:
        async with asyncio.timeout(float(os.getenv("RUN_TIMEOUT_SECONDS", "180"))):
            async with client_factory(options=options) as client:
                watcher = asyncio.create_task(watch_cancel(client))
                try:
                    await client.query(spec["prompt"])
                    with (run / "events.jsonl").open("a") as events:
                        async for message in client.receive_response():
                            events.write(json.dumps(encode(message)) + "\n")
                            events.flush()
                            if isinstance(message, ResultMessage):
                                result = {"status": "failed" if message.is_error else "succeeded",
                                          "result": message.result, "structured_output": message.structured_output, "error": message.subtype if message.is_error else None}
                                if schema is not None and not message.is_error:
                                    Draft7Validator(schema).validate(message.structured_output)
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
    except TimeoutError:
        result = {"status": "timed_out", "error": "Execution exceeded its time limit", "result": None}
    except Exception as error:
        result = {"status": "failed", "error": f"{type(error).__name__}: {error}", "result": None}
    if (run / "cancel").exists():
        result["status"] = "cancelled"
    atomic_json(run / "result.json", result)


if __name__ == "__main__":
    asyncio.run(execute(Path(os.getenv("SESSION_DIR", "/session")), sys.argv[1]))
