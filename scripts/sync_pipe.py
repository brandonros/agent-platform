#!/usr/bin/env python3
"""Install/update the lab pipe through Open WebUI's admin API."""
from pathlib import Path
import os
import json
import urllib.request


def call(method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        os.getenv("OWUI_URL", "http://chat.127.0.0.1.sslip.io") + path,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)



def main():
    token = call("POST", "/api/v1/auths/signin", body={
        "email": os.environ["OWUI_EMAIL"], "password": os.environ["OWUI_PASSWORD"]})["token"]
    import json
    root = Path(__file__).resolve().parents[1]
    functions = call("GET", "/api/v1/functions/", token)
    models = {"org_claude_code": ("Haiku 5.5", "claude-haiku-5-5"),
              "org_claude_sonnet": ("Sonnet 5.5", "claude-sonnet-5-5"),
              "org_claude_opus": ("Opus 5.5", "claude-opus-5-5"),
              "org_claude_fable": ("Fable 5.1", "claude-fable-5-1")}
    for pipe_id, (label, model) in models.items():
        existing = next((f for f in functions if f["id"] == pipe_id), None)
        body = {"id": pipe_id, "name": "Organizational Claude — " + label,
                "meta": {"description": "Shared organizational Claude setup; model: " + model},
                "content": (root / "apps/open-webui-pipe-extension/pipe.py").read_text()}
        path = f"/api/v1/functions/id/{pipe_id}/update" if existing else "/api/v1/functions/create"
        result = call("POST", path, token, body)
        valves = call("GET", f"/api/v1/functions/id/{pipe_id}/valves", token) or {}
        valves.update(AGENT="general", MODEL=model, SERVICE_URL="http://claude-runtime-server.chat.svc.cluster.local:8000")
        call("POST", f"/api/v1/functions/id/{pipe_id}/valves/update", token, valves)
        if not result["is_active"]:
            call("POST", f"/api/v1/functions/id/{pipe_id}/toggle", token)
        print("Installed", body["name"])
    # Preserve historical chats, but remove role entries from the active selector.
    for function in functions:
        if function["id"] in ("org_claude_writer", "org_claude_reviewer") and function["is_active"]:
            call("POST", f"/api/v1/functions/id/{function['id']}/toggle", token)


if __name__ == "__main__":
    main()
