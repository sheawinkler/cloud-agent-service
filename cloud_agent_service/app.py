from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cloud_agent_service.models import DeploymentPolicy, JobRequest, PromotionStatus, RepoProvider
from cloud_agent_service.orchestrator import LocalJobQueue, LocalOrchestrator
from cloud_agent_service.pipeline import AgentCloudFlow, RequestValidationError
from cloud_agent_service.store import JobStore


class CreateJobPayload(BaseModel):
    prompt: str = Field(min_length=1)
    repo_path: str = ""
    repo_provider: RepoProvider = RepoProvider.LOCAL
    git_url: str | None = None
    github_repo: str | None = None
    parent_job_id: str | None = None
    model_id: str = "local-deterministic"
    agent_id: str = "repo-editor-v1"
    user_id: str = "local-user"
    base_branch: str = "main"
    deploy_policy: DeploymentPolicy = DeploymentPolicy.MANUAL
    token_budget: int = 8_000
    max_prompt_chars: int = 8_000
    max_runtime_seconds: int = 600
    max_changed_files: int = 12
    run_immediately: bool = True


def build_flow() -> AgentCloudFlow:
    runtime_root = Path(os.environ.get("AGENT_CLOUD_RUNTIME", ".runtime"))
    store = JobStore(os.environ.get("AGENT_CLOUD_DB", str(runtime_root / "jobs.sqlite3")))
    return AgentCloudFlow(
        store=store,
        workspace_root=os.environ.get("AGENT_CLOUD_WORKSPACES", str(runtime_root / "workspaces")),
        artifacts_dir=os.environ.get("AGENT_CLOUD_ARTIFACTS", str(runtime_root / "artifacts")),
    )


flow = build_flow()
job_queue = LocalJobQueue()
orchestrator = LocalOrchestrator(flow, job_queue)
app = FastAPI(title="Cloud Agent Service MVP", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/integrations/github/status")
def github_status() -> dict[str, Any]:
    return asdict(flow.github_status())


@app.post("/jobs")
def create_job(payload: CreateJobPayload, background_tasks: BackgroundTasks) -> dict[str, Any]:
    request = _job_request_from_payload(payload)
    try:
        job_id = flow.create_job(request)
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    orchestrator.submit(job_id)
    if payload.run_immediately:
        background_tasks.add_task(orchestrator.run_queued_once)

    return {"job_id": job_id, "status": "queued"}


@app.post("/run-code-job")
def run_code_job(payload: CreateJobPayload) -> dict[str, Any]:
    request = _job_request_from_payload(payload)
    try:
        job_id = flow.create_job(request)
        result = flow.run_job(job_id)
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


@app.get("/jobs")
def list_jobs(limit: int = 50, user_id: str | None = None) -> dict[str, Any]:
    return {"jobs": flow.store.list_jobs(limit=limit, user_id=user_id)}


@app.get("/lab/runs")
def list_lab_runs(
    limit: int = 50,
    model_id: str | None = None,
    agent_id: str | None = None,
    promotion_status: PromotionStatus | None = None,
) -> dict[str, Any]:
    return {
        "runs": flow.store.list_lab_runs(
            limit=limit,
            model_id=model_id,
            agent_id=agent_id,
            promotion_status=promotion_status.value if promotion_status else None,
        )
    }


@app.get("/lab/summary")
def lab_summary() -> dict[str, Any]:
    return flow.store.lab_summary()


@app.post("/jobs/run-next")
def run_next_job() -> dict[str, Any]:
    result = flow.run_next_queued_job()
    if result is None:
        return {"status": "idle"}
    return asdict(result)


@app.post("/jobs/{job_id}/run")
def run_job(job_id: str) -> dict[str, Any]:
    job = flow.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    result = flow.run_job(job_id)
    return asdict(result)


@app.get("/jobs/{job_id}/worker-payload")
def get_worker_payload(job_id: str) -> dict[str, Any]:
    try:
        payload = flow.build_worker_payload(job_id, status_callback_url="local://jobs")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return asdict(payload)


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        cancelled = flow.cancel_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    if not cancelled:
        raise HTTPException(status_code=409, detail="job is already running or finished")
    return {"job_id": job_id, "status": "cancelled"}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict[str, Any]:
    try:
        retried = flow.retry_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    if not retried:
        raise HTTPException(status_code=409, detail="only failed or cancelled jobs can be retried")
    orchestrator.submit(job_id)
    return {"job_id": job_id, "status": "queued"}


class ContinueJobPayload(BaseModel):
    prompt: str = Field(min_length=1)
    token_budget: int | None = None
    run_immediately: bool = True


@app.post("/jobs/{job_id}/continue")
def continue_job(
    job_id: str,
    payload: ContinueJobPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    parent = flow.store.get_job(job_id)
    if not parent:
        raise HTTPException(status_code=404, detail="job not found")
    request = JobRequest(
        prompt=payload.prompt,
        repo_path=parent["repo_path"],
        repo_provider=RepoProvider(parent["repo_provider"]),
        git_url=parent["git_url"],
        github_repo=parent["github_repo"],
        parent_job_id=job_id,
        model_id=parent["model_id"],
        agent_id=parent["agent_id"],
        user_id=parent["user_id"],
        base_branch=parent["base_branch"],
        deploy_policy=DeploymentPolicy(parent["deploy_policy"]),
        token_budget=payload.token_budget or parent["token_budget"],
        max_changed_files=parent["max_changed_files"],
        max_runtime_seconds=parent["max_runtime_seconds"],
        max_prompt_chars=parent["max_prompt_chars"],
    )
    try:
        child_job_id = flow.create_job(request)
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.run_immediately:
        result = flow.run_job(child_job_id)
        response = asdict(result)
        response["parent_job_id"] = job_id
        return response
    orchestrator.submit(child_job_id)
    background_tasks.add_task(orchestrator.run_queued_once)
    return {"job_id": child_job_id, "parent_job_id": job_id, "status": "queued"}


def _job_request_from_payload(payload: CreateJobPayload) -> JobRequest:
    return JobRequest(
        prompt=payload.prompt,
        repo_path=payload.repo_path,
        repo_provider=payload.repo_provider,
        git_url=payload.git_url,
        github_repo=payload.github_repo,
        parent_job_id=payload.parent_job_id,
        model_id=payload.model_id,
        agent_id=payload.agent_id,
        user_id=payload.user_id,
        base_branch=payload.base_branch,
        deploy_policy=payload.deploy_policy,
        token_budget=payload.token_budget,
        max_prompt_chars=payload.max_prompt_chars,
        max_runtime_seconds=payload.max_runtime_seconds,
        max_changed_files=payload.max_changed_files,
    )


@app.post("/jobs/{job_id}/approve-deployment")
def approve_deployment(job_id: str) -> dict[str, Any]:
    try:
        result = flow.approve_deployment(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except RequestValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asdict(result)


@app.get("/jobs/{job_id}/budget")
def get_budget(job_id: str) -> dict[str, Any]:
    job = flow.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    entries = flow.store.list_budget_entries(job_id)
    return {
        "job_id": job_id,
        "token_budget": job["token_budget"],
        "tokens_used": flow.store.budget_tokens_used(job_id),
        "entries": entries,
    }


@app.get("/jobs/{job_id}/events")
def get_job_events(job_id: str, after_id: int = 0) -> dict[str, Any]:
    job = flow.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"events": flow.store.list_events_after(job_id, after_id)}


@app.get("/jobs/{job_id}/events/stream")
def stream_job_events(
    job_id: str,
    after_id: int = 0,
    timeout_seconds: int = 30,
) -> StreamingResponse:
    if not flow.store.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")

    def event_stream():
        last_id = after_id
        deadline = time.monotonic() + max(1, min(timeout_seconds, 120))
        while time.monotonic() < deadline:
            events = flow.store.list_events_after(job_id, last_id)
            for event in events:
                last_id = event["id"]
                yield f"data: {json.dumps(event, sort_keys=True)}\n\n"
            job = flow.store.get_job(job_id)
            if job and job["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.25)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = flow.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    job["events"] = flow.store.list_events(job_id)
    return job
