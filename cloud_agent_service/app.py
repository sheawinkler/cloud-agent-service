from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
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


@app.post("/jobs/{job_id}/run")
def run_job(job_id: str) -> dict[str, Any]:
    job = flow.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    result = flow.run_job(job_id)
    return asdict(result)


@app.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = flow.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    job["events"] = flow.store.list_events(job_id)
    return job
