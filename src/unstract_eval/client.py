"""Minimal client for one Unstract API deployment.

Follows the execute/status contract in Zipstack/unstract-python-client's
OpenAPI spec (specs/docstudio-oss.json):

* ``POST {api_url}`` with multipart ``files`` starts an execution. ``timeout=0``
  queues it and returns at once with ``message.execution_id`` and
  ``message.status_api``.
* ``GET {api_url}?execution_id=...`` answers 422 while running, 200 once done,
  and 406 if the finished result was already read (reads are one-shot).

``llm_profile_id`` overrides the deployment's LLM profile per request, which is
what lets one golden set be run against several profiles.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

_RUNNING = {"PENDING", "QUEUED", "INITIATED", "EXECUTING", "IN_PROGRESS", "RUNNING", "STARTED"}
_FAILED = {"ERROR", "FAILED", "FAILURE", "STOPPED"}


class UnstractError(RuntimeError):
    pass


@dataclass
class Extraction:
    file: str
    output: dict | None
    status: str
    error: str | None
    cost: float | None
    seconds: float
    raw: dict


class DeploymentClient:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        poll_interval: float = 2.0,
        max_wait: float = 600.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self.api_url = api_url if api_url.endswith("/") else api_url + "/"
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self._sleep = sleep
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, read=330.0),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def extract(self, path: Path, llm_profile_id: str | None = None) -> Extraction:
        started = time.monotonic()
        data: dict[str, Any] = {"timeout": "0", "include_metadata": "true"}
        if llm_profile_id:
            data["llm_profile_id"] = llm_profile_id
        with open(path, "rb") as fh:
            resp = self._http.post(self.api_url, data=data, files=[("files", (path.name, fh))])
        if resp.status_code >= 400:
            raise UnstractError(f"{path.name}: execute returned {resp.status_code}: {resp.text[:300]}")
        message = resp.json().get("message") or {}
        results = message.get("result")
        status = str(message.get("execution_status", "")).upper()
        if status in _FAILED:
            raise UnstractError(f"{path.name}: execution {status}: {message.get('error')}")
        if not results or status in _RUNNING:
            results = self._wait(message)
        file_result = results[0] if results else {}
        return Extraction(
            file=path.name,
            output=extract_output(file_result),
            status=str(file_result.get("status", "")),
            error=file_result.get("error"),
            cost=extract_cost(file_result),
            seconds=round(time.monotonic() - started, 2),
            raw=file_result,
        )

    def _wait(self, message: dict) -> list[dict]:
        status_api = message.get("status_api")
        if status_api:
            url = urljoin(self.api_url, status_api)
            params = None
        else:
            url, params = self.api_url, {"execution_id": message["execution_id"]}
        waited = 0.0
        while waited <= self.max_wait:
            resp = self._http.get(url, params=params)
            body = resp.json() if resp.content else {}
            state = str(body.get("status", "")).upper()
            if resp.status_code == 200:
                return body.get("message") or []
            if resp.status_code == 406:
                raise UnstractError("result already consumed by an earlier status read (reads are one-shot)")
            if resp.status_code == 422 and state in _FAILED:
                raise UnstractError(f"execution {state}: {json.dumps(body)[:300]}")
            if resp.status_code not in (422, 429, 502, 503, 504):
                raise UnstractError(f"status returned {resp.status_code}: {resp.text[:300]}")
            self._sleep(self.poll_interval)
            waited += self.poll_interval
        raise UnstractError(f"execution {message.get('execution_id')} still running after {self.max_wait}s")


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def extract_output(file_result: dict) -> dict | None:
    """Pull the extracted fields out of one FileResult.

    The docs show more than one shape for ``result`` (``{"output": {...}}``,
    ``{"input_file": ..., "result": ...}``, or a JSON string), so accept each.
    """
    r = _maybe_json(file_result.get("result"))
    for _ in range(3):
        if isinstance(r, dict) and "output" in r:
            r = _maybe_json(r["output"])
        elif isinstance(r, dict) and "result" in r and set(r) <= {"result", "input_file", "metadata"}:
            r = _maybe_json(r["result"])
        else:
            break
    return r if isinstance(r, dict) else None


def extract_cost(file_result: dict) -> float | None:
    """Sum every ``cost_in_dollars`` found anywhere in the result metadata."""
    total, found = 0.0, False

    def walk(node: Any) -> None:
        nonlocal total, found
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "cost_in_dollars":
                    try:
                        total += float(v)
                        found = True
                    except (TypeError, ValueError):
                        pass
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(_maybe_json(file_result.get("result")))
    walk(file_result.get("metadata"))
    return round(total, 6) if found else None
