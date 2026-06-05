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

from cloud_agent_service.models import DeploymentPolicy, JobRequest
from cloud_agent_service.orchestrator import LocalJobQueue, LocalOrchestrator
from cloud_agent_service.pipeline import AgentCloudFlow, RequestValidationError
from cloud_agent_service.store import JobStore


class CreateJobPayload(BaseModel):
    prompt: str = Field(min_length=1)
    repo_path: str
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
app = FastAPI(title="Cloud Agent Service Local MVP", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/integrations/github/status")
def github_status() -> dict[str, Any]:
    return asdict(flow.github_status())


@app.post("/jobs")
def create_job(payload: CreateJobPayload, background_tasks: BackgroundTasks) -> dict[str, Any]:
    request = JobRequest(
        prompt=payload.prompt,
        repo_path=payload.repo_path,
        user_id=payload.user_id,
        base_branch=payload.base_branch,
        deploy_policy=payload.deploy_policy,
        token_budget=payload.token_budget,
        max_prompt_chars=payload.max_prompt_chars,
        max_runtime_seconds=payload.max_runtime_seconds,
        max_changed_files=payload.max_changed_files,
    )
    try:
        job_id = flow.create_job(request)
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    orchestrator.submit(job_id)
    if payload.run_immediately:
        background_tasks.add_task(orchestrator.run_queued_once)

    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs")
def list_jobs(limit: int = 50, user_id: str | None = None) -> dict[str, Any]:
    return {"jobs": flow.store.list_jobs(limit=limit, user_id=user_id)}


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
