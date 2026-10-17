"""Small standard-library HTTP client; no Temporal or Kubernetes dependency."""
import argparse
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


class Client:
    def __init__(self):
        self.url = os.getenv("WORKFLOW_API_URL", "http://workflows.127.0.0.1.sslip.io").rstrip("/")
        self.token = os.environ.get("WORKFLOW_API_TOKEN")
        if not self.token:
            raise SystemExit("Export WORKFLOW_API_TOKEN before running this client.")

    def request(self, method, path, body=None, key=None):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = Request(self.url + path, method=method, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise SystemExit(f"HTTP {error.code}: {error.read().decode()}") from None

    def start(self, workflow_type, queue, args, key=None):
        return self.request("POST", "/workflows",
                            {"type": workflow_type, "task_queue": queue, "args": args}, key)

    def get(self, workflow_id):
        return self.request("GET", "/workflows/" + quote(workflow_id, safe=""))

    def signal(self, workflow_id, name, args):
        return self.request("POST", "/workflows/" + quote(workflow_id, safe="") + "/signals",
                            {"name": name, "args": args})

    def wait(self, workflow_id, timeout=300):
        deadline = time.monotonic() + timeout
        while True:
            result = self.get(workflow_id)
            if result["status"] != "running":
                return result
            if time.monotonic() >= deadline:
                raise SystemExit(f"Still running: {workflow_id}. Poll again later; the workflow continues.")
            time.sleep(2)


def display(value):
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("type")
    start.add_argument("queue")
    start.add_argument("args", nargs="?", default="[]", type=json.loads)
    start.add_argument("--key", help="Stable idempotency key for retrying the same start")
    show = commands.add_parser("show")
    show.add_argument("id")
    wait = commands.add_parser("wait")
    wait.add_argument("id")
    wait.add_argument("--timeout", type=float, default=300)
    signal = commands.add_parser("signal")
    signal.add_argument("id")
    signal.add_argument("name")
    signal.add_argument("args", nargs="?", default="[]", type=json.loads)
    options = parser.parse_args()
    client = Client()
    if options.command == "start":
        display(client.start(options.type, options.queue, options.args, options.key))
    elif options.command == "show":
        display(client.get(options.id))
    elif options.command == "wait":
        display(client.wait(options.id, options.timeout))
    else:
        display(client.signal(options.id, options.name, options.args))
