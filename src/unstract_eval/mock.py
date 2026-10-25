"""Offline stand-in for an Unstract API deployment.

Implements the same async contract the real endpoint documents (POST queues,
GET answers 422 while running, 200 once, then 406), serving canned outputs per
LLM profile from a fixture file. Used by the tests and by ``--mock`` so the
harness can be demoed without an API key. Every number it returns, including
cost, comes from the fixture and is simulated.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import httpx


class MockUnstract:
    def __init__(self, fixture: dict, api_key: str = "mock-key", running_polls: int = 1) -> None:
        self.fixture = fixture
        self.api_key = api_key
        self.running_polls = running_polls
        self.executions: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []

    @classmethod
    def from_file(cls, path: str | Path, **kw) -> "MockUnstract":
        return cls(json.loads(Path(path).read_text()), **kw)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"errors": [{"code": "authentication_failed"}]})
        if request.method == "POST":
            return self._execute(request)
        if request.method == "GET":
            return self._status(request)
        return httpx.Response(405)

    def _execute(self, request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8", "replace")
        m = re.search(r'name="files"; filename="([^"]+)"', body)
        if not m:
            return httpx.Response(400, json={"errors": [{"detail": "files required"}]})
        filename = m.group(1)
        p = re.search(r'name="llm_profile_id"\r\n\r\n([^\r\n]*)', body)
        profile = p.group(1) if p else self.fixture.get("default_profile", "default")
        outputs = self.fixture["profiles"].get(profile)
        if outputs is None:
            return httpx.Response(400, json={"errors": [{"detail": f"unknown llm_profile_id {profile}"}]})
        exec_id = str(uuid.uuid4())
        self.executions[exec_id] = {"file": filename, "profile": profile, "polls": 0, "read": False}
        path = request.url.path
        return httpx.Response(200, json={"message": {
            "execution_id": exec_id,
            "execution_status": "PENDING",
            "status_api": f"{path}?execution_id={exec_id}",
            "error": None,
            "result": None,
        }})

    def _status(self, request: httpx.Request) -> httpx.Response:
        exec_id = request.url.params.get("execution_id")
        ex = self.executions.get(exec_id or "")
        if ex is None:
            return httpx.Response(404, json={"errors": [{"code": "not_found"}]})
        if ex["read"]:
            return httpx.Response(406, json={"status": "ACKNOWLEDGED"})
        if ex["polls"] < self.running_polls:
            ex["polls"] += 1
            return httpx.Response(422, json={"status": "EXECUTING", "message": None})
        ex["read"] = True
        profile = self.fixture["profiles"][ex["profile"]]
        output = profile.get(ex["file"])
        cost = self.fixture.get("cost_per_doc", {}).get(ex["profile"])
        item = {
            "file": ex["file"],
            "file_execution_id": str(uuid.uuid4()),
            "status": "Success" if output is not None else "Failed",
            "error": None if output is not None else "No output for file",
            "result": {
                "output": output,
                "metadata": {"extraction_llm": [{"cost_in_dollars": cost}]} if cost is not None else {},
            } if output is not None else None,
            "metadata": {"source_name": ex["file"]},
        }
        return httpx.Response(200, json={"status": "COMPLETED", "message": [item]})
