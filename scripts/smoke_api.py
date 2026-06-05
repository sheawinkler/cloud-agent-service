from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass
class SmokeResult:
    name: str
    ok: bool
    detail: dict[str, Any]


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", path, payload)

    def stream_first_line(self, path: str) -> str:
        request = Request(self.base_url + path, method="GET")
        with urlopen(request, timeout=5) as response:
            return response.readline().decode("utf-8").strip()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                data = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8")
            raise RuntimeError(f"{method} {path} failed: {exc.code} {detail}") from exc
        return json.loads(data)


def record(results: list[SmokeResult], name: str, ok: bool, **detail: Any) -> None:
    results.append(SmokeResult(name=name, ok=ok, detail=detail))


def run_smoke(base_url: str, repo_path: str) -> dict[str, Any]:
    client = ApiClient(base_url)
    results: list[SmokeResult] = []

    health = client.get("/health")
    record(results, "health", health == {"status": "ok"}, response=health)

    github = client.get("/integrations/github/status")
    record(
        results,
        "github_status",
        "configured" in github and "mode" in github,
        response=github,
    )

    job = client.post(
        "/jobs",
        {
            "prompt": "For my shopping website, create a buy button.",
            "repo_path": repo_path,
            "deploy_policy": "manual",
            "run_immediately": False,
            "max_changed_files": 2,
            "token_budget": 2000,
        },
    )
    job_id = job["job_id"]
    record(results, "job_created", job["status"] == "queued", job_id=job_id)

    payload = client.get(f"/jobs/{job_id}/worker-payload")
    record(
        results,
        "worker_payload",
        payload["token_budget"] == 2000
        and payload["max_changed_files"] == 2
        and payload["working_branch"] == f"agent/{job_id}",
        token_budget=payload["token_budget"],
        max_changed_files=payload["max_changed_files"],
    )

    run = client.post("/jobs/run-next")
    record(
        results,
        "run_next_manual",
        run["status"] == "succeeded"
        and run["deployment_status"] == "ready: manual approval required"
        and run["tests_failed"] == [],
        status=run["status"],
        deployment_status=run["deployment_status"],
    )

    budget = client.get(f"/jobs/{job_id}/budget")
    record(
        results,
        "budget",
        budget["tokens_used"] > 0 and len(budget["entries"]) >= 1,
        tokens_used=budget["tokens_used"],
        entries=len(budget["entries"]),
    )

    events = client.get(f"/jobs/{job_id}/events")
    event_types = [event["event_type"] for event in events["events"]]
    record(
        results,
        "events",
        "repo_analyzed" in event_types and "budget_charged" in event_types,
        tail=event_types[-5:],
    )

    first_line = client.stream_first_line(f"/jobs/{job_id}/events/stream")
    record(
        results,
        "events_stream",
        first_line.startswith("data: "),
        first_line=first_line,
    )

    approved = client.post(f"/jobs/{job_id}/approve-deployment")
    record(
        results,
        "approve_deployment",
        approved["deployment_status"] == "deployed: local mock deployment recorded",
        deployment_status=approved["deployment_status"],
    )

    tiny = client.post(
        "/jobs",
        {
            "prompt": "For my shopping website, create a buy button.",
            "repo_path": repo_path,
            "deploy_policy": "local",
            "run_immediately": False,
            "token_budget": 10,
        },
    )
    tiny_run = client.post(f"/jobs/{tiny['job_id']}/run")
    record(
        results,
        "budget_stop",
        tiny_run["status"] == "failed"
        and tiny_run["deployment_status"] == "not deployed: budget exceeded"
        and tiny_run["pr_url"] is None,
        status=tiny_run["status"],
        deployment_status=tiny_run["deployment_status"],
        pr_url=tiny_run["pr_url"],
    )

    passed = [result for result in results if result.ok]
    return {
        "ok": len(passed) == len(results),
        "passed": len(passed),
        "total": len(results),
        "results": [result.__dict__ for result in results],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run API smoke tests against a live service.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--repo-path", default="/host_repo")
    args = parser.parse_args()

    payload = run_smoke(args.base_url, args.repo_path)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not payload["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
