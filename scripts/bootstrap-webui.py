"""Initialize lab WebUI accounts, pipes and scoped model grants."""
import base64
import json
import os
import secrets
import subprocess
from urllib.parse import urlencode
from sync_pipe import call, install


def kube(*args, body=None):
    return subprocess.check_output(["kubectl", "-n", "chat", *args], input=body)


def main():
    secret = json.loads(kube("get", "secret", "temporal-webui-login", "--ignore-not-found", "-o", "json") or b"null")
    saved = {k: base64.b64decode(v).decode() for k, v in secret["data"].items()} if secret else None
    email = os.getenv("OWUI_EMAIL") or (saved or {}).get("email")
    password = os.getenv("OWUI_PASSWORD") or (saved or {}).get("password")
    if not email or not password:
        raise SystemExit("Set OWUI_EMAIL and OWUI_PASSWORD for the WebUI administrator.")
    if call("GET", "/api/config").get("onboarding"):
        admin = call("POST", "/api/v1/auths/signup", body={
            "email": email, "password": password, "name": "Lab administrator"})
    else:
        admin = call("POST", "/api/v1/auths/signin", body={"email": email, "password": password})
    if admin.get("role") != "admin":
        raise SystemExit("OWUI_EMAIL/OWUI_PASSWORD must identify an administrator. No roles changed.")
    token = admin["token"]
    if not saved:
        saved = {"email": os.getenv("WORKER_EMAIL", "workflow@lab.example.com"),
                 "password": secrets.token_urlsafe(32)}
        # Save before creation so interrupted setup can resume without losing credentials.
        kube("apply", "-f", "-", body=json.dumps({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "temporal-webui-login", "namespace": "chat"},
            "type": "Opaque", "stringData": saved}).encode())
    users = call("GET", "/api/v1/users/?" + urlencode({"query": saved["email"]}), token)["users"]
    account = next((u for u in users if u["email"].lower() == saved["email"].lower()), None)
    if not account:
        account = call("POST", "/api/v1/auths/add", token, {
            **saved, "name": "Temporal workflows", "role": "user"})
    call("POST", "/api/v1/auths/signin", body=saved)
    install(token, account["id"])
    print("WebUI ready; workflow credentials preserved; scoped model grants configured.")
    if account["role"] == "admin":
        print("Existing admin workflow login preserved. Fresh installs use a non-admin login.")


if __name__ == "__main__":
    main()
