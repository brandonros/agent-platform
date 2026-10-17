"""Small Kubernetes REST client. Job names are stable across API restarts."""
import os
import json
from pathlib import Path
import ssl

import httpx


class KubernetesJobs:
    def __init__(self):
        self.namespace = os.getenv("JOB_NAMESPACE", "chat")
        self.client = None

    async def request(self, method, path, **kwargs):
        if self.client is None:
            account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
            self.client = httpx.AsyncClient(
                base_url="https://kubernetes.default.svc", timeout=15,
                verify=ssl.create_default_context(cafile=str(account / "ca.crt")),
                headers={"Authorization": "Bearer " + (account / "token").read_text().strip()},
            )
        return await self.client.request(method, path, **kwargs)

    @property
    def path(self):
        return f"/apis/batch/v1/namespaces/{self.namespace}/jobs"

    async def submit(self, run_id, session_id, session_path):
        response = await self.request("POST", self.path, json=self.manifest(run_id, session_id))
        if response.status_code != 409:
            response.raise_for_status()

    def manifest(self, run_id, session_id):
        template = json.loads(Path(os.environ["JOB_TEMPLATE_PATH"]).read_text())
        def fill(value):
            if isinstance(value, str):
                return value.replace("${RUN_ID}", run_id).replace("${SESSION_ID}", session_id)
            if isinstance(value, list):
                return [fill(item) for item in value]
            if isinstance(value, dict):
                return {key: fill(item) for key, item in value.items()}
            return value
        return fill(template)

    async def status(self, run_id):
        response = await self.request("GET", f"{self.path}/claude-run-{run_id}")
        if response.status_code == 404:
            # Foreground deletion must finish before releasing the session lock.
            return "missing"
        response.raise_for_status()
        body = response.json()
        if body["metadata"].get("deletionTimestamp"):
            return "running"
        conditions = body.get("status", {}).get("conditions", [])
        for condition in conditions:
            if condition.get("status") == "True" and condition["type"] in ("Complete", "Failed"):
                return "succeeded" if condition["type"] == "Complete" else "failed"
        return "running"

    async def cancel(self, run_id):
        response = await self.request("DELETE", f"{self.path}/claude-run-{run_id}", json={"propagationPolicy": "Foreground", "gracePeriodSeconds": 10})
        if response.status_code != 404:
            response.raise_for_status()

    async def close(self):
        if self.client:
            await self.client.aclose()
