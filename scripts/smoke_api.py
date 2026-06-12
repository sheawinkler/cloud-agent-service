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

    def get_text(self, path: str) -> str:
        request = Request(self.base_url + path, method="GET")
        with urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

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

    auth = client.get("/auth/status")
    record(
        results,
        "auth_status",
        auth["api_key_required"] is False and "user_token_quota" in auth,
        response=auth,
    )

    cloud = client.get("/integrations/cloud/status")
    record(
        results,
        "cloud_status",
        cloud["provider"] == "aws-ecs" and "configured" in cloud,
        response=cloud,
    )

    models = client.get("/models")
    record(
        results,
        "models",
        any(model["model_id"] == "local-deterministic" for model in models["models"])
        and any(agent["agent_id"] == "openai-repo-editor-v1" for agent in models["agents"]),
        models=len(models["models"]),
        agents=len(models["agents"]),
    )

    harnesses = client.get("/harnesses")
    top_harness_ids = {harness["harness_id"] for harness in harnesses["top_20"]}
    record(
        results,
        "harnesses",
        len(harnesses["top_20"]) == 20
        and {
            "factory-droid",
            "pi-coding-agent",
            "hermes-agent",
            "openai-codex-cli",
        }.issubset(top_harness_ids)
        and any(
            harness["harness_id"] == "agno"
            for harness in harnesses["harnesses"]
        )
        and harnesses["custom_harness_prefix"] == "custom:",
        indexed=len(harnesses["harnesses"]),
        top_20=len(harnesses["top_20"]),
        custom_harness_prefix=harnesses["custom_harness_prefix"],
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
        and payload["working_branch"] == f"agent/{job_id}"
        and payload["model_id"] == "local-deterministic"
        and payload["agent_id"] == "repo-editor-v1"
        and payload["harness_id"] == "local-template",
        token_budget=payload["token_budget"],
        max_changed_files=payload["max_changed_files"],
    )

    run = client.post(f"/jobs/{job_id}/run")
    record(
        results,
        "run_manual",
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

    lab_runs = client.get("/lab/runs?promotion_status=promote")
    record(
        results,
        "lab_runs",
        any(
            run["job_id"] == job_id
            and run["model_id"] == "local-deterministic"
            and run["agent_id"] == "repo-editor-v1"
            and run["harness_id"] == "local-template"
            and run["promotion_status"] == "promote"
            for run in lab_runs["runs"]
        ),
        returned=len(lab_runs["runs"]),
    )

    lab_summary = client.get("/lab/summary")
    record(
        results,
        "lab_summary",
        lab_summary["total_runs"] >= 1
        and lab_summary["by_promotion_status"].get("promote", 0) >= 1,
        response=lab_summary,
    )

    lab_ui = client.get_text("/lab")
    record(
        results,
        "lab_ui",
        "<title>Agent Lab</title>" in lab_ui and "Recent Runs" in lab_ui,
        length=len(lab_ui),
    )

    quota = client.get("/users/local-user/quota")
    record(
        results,
        "user_quota",
        quota["jobs_count"] >= 1 and quota["token_budget_reserved"] >= 2000,
        response=quota,
    )

    one_click = client.post(
        "/run-code-job",
        {
            "prompt": "For my shopping website, create a buy button.",
            "repo_path": repo_path,
            "deploy_policy": "preview_only",
            "max_changed_files": 2,
            "token_budget": 2000,
        },
    )
    one_click_job_id = one_click["job_id"]
    evidence = one_click.get("evidence", {})
    record(
        results,
        "run_code_job",
        one_click["status"] == "succeeded"
        and one_click["deployment_status"] == "ready: preview only"
        and evidence.get("browser_checks", {}).get("buy_button_present") is True
        and one_click.get("promotion_decision", {}).get("status") == "needs_review",
        status=one_click["status"],
        deployment_status=one_click["deployment_status"],
        preview_url=evidence.get("preview_url"),
    )

    continuation = client.post(
        f"/jobs/{one_click_job_id}/continue",
        {
            "prompt": "Make the buy button more prominent.",
            "run_immediately": True,
        },
    )
    continuation_payload = client.get(f"/jobs/{continuation['job_id']}/worker-payload")
    record(
        results,
        "continue_job",
        continuation["status"] == "succeeded"
        and continuation_payload["parent_job_id"] == one_click_job_id
        and continuation_payload["working_branch"] == f"agent/{one_click_job_id}",
        status=continuation["status"],
        parent_job_id=continuation_payload["parent_job_id"],
        working_branch=continuation_payload["working_branch"],
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
