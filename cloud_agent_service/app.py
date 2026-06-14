from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from cloud_agent_service.cloud_dispatch import EcsDispatchPlanner
from cloud_agent_service.models import (
    DeploymentPolicy,
    JobRequest,
    PromotionStatus,
    RepoProvider,
    RoutingPolicy,
    WorkerCallbackType,
)
from cloud_agent_service.orchestrator import LocalJobQueue, LocalOrchestrator
from cloud_agent_service.pipeline import AgentCloudFlow, RequestValidationError
from cloud_agent_service.store import JobStore
from cloud_agent_service.task_corpus import default_replayable_corpus


class CreateJobPayload(BaseModel):
    prompt: str = Field(min_length=1)
    repo_path: str = ""
    repo_provider: RepoProvider = RepoProvider.LOCAL
    git_url: str | None = None
    github_repo: str | None = None
    parent_job_id: str | None = None
    model_id: str = "local-deterministic"
    agent_id: str = "repo-editor-v1"
    harness_id: str = "local-template"
    user_id: str = "local-user"
    base_branch: str = "main"
    deploy_policy: DeploymentPolicy = DeploymentPolicy.MANUAL
    routing_policy: RoutingPolicy = RoutingPolicy.FIXED
    token_budget: int = 8_000
    max_prompt_chars: int = 8_000
    max_runtime_seconds: int = 600
    max_changed_files: int = 12
    run_immediately: bool = True


class CreateExperimentPayload(BaseModel):
    case_id: str = Field(min_length=1)
    name: str | None = None
    model_ids: list[str] | None = None
    agent_ids: list[str] | None = None
    harness_ids: list[str] | None = None
    task_ids: list[str] | None = None
    notes: str = ""


class RunExperimentPayload(BaseModel):
    repo_path: str = Field(min_length=1)
    deploy_policy: DeploymentPolicy = DeploymentPolicy.PREVIEW_ONLY


class RunExperimentBatchPayload(RunExperimentPayload):
    max_concurrency: int = 1


class DatasetExportPayload(BaseModel):
    export_id: str | None = None
    limit: int = 200
    promotion_status: PromotionStatus | None = None


class RouterRecommendPayload(BaseModel):
    prompt: str = Field(min_length=1)
    repo_path: str = ""
    repo_provider: RepoProvider = RepoProvider.LOCAL
    git_url: str | None = None
    github_repo: str | None = None
    model_id: str = "local-deterministic"
    agent_id: str = "repo-editor-v1"
    harness_id: str = "local-template"
    deploy_policy: DeploymentPolicy = DeploymentPolicy.MANUAL
    routing_policy: RoutingPolicy = RoutingPolicy.RECOMMEND_ONLY


class WorkerCallbackPayload(BaseModel):
    callback_type: WorkerCallbackType
    status: str = "ok"
    payload: dict[str, Any] = Field(default_factory=dict)


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
ecs_dispatch_planner = EcsDispatchPlanner()
app = FastAPI(title="Cloud Agent Service MVP", version="0.1.0")


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    keys = _configured_api_keys()
    public_paths = {"/health", "/auth/status"}
    if keys and request.url.path not in public_paths:
        if request.headers.get("x-api-key") not in keys:
            return JSONResponse(
                status_code=401,
                content={"detail": "valid x-api-key header required"},
            )
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/auth/status")
def auth_status() -> dict[str, Any]:
    return {
        "api_key_required": bool(_configured_api_keys()),
        "user_token_quota": _user_token_quota(),
    }


@app.get("/integrations/github/status")
def github_status() -> dict[str, Any]:
    return asdict(flow.github_status())


@app.get("/integrations/cloud/status")
def cloud_status() -> dict[str, Any]:
    return ecs_dispatch_planner.status()


@app.get("/models")
def model_agent_status() -> dict[str, Any]:
    return flow.model_agent_status()


@app.get("/harnesses")
def harness_status() -> dict[str, Any]:
    return flow.harness_status()


@app.get("/harnesses/{harness_id:path}")
def get_harness(harness_id: str) -> dict[str, Any]:
    try:
        return asdict(flow.harness_registry.get(harness_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="harness not found") from exc


@app.get("/analysis/cases")
def list_analysis_cases() -> dict[str, Any]:
    return {"cases": flow.list_analysis_cases()}


@app.get("/analysis/cases/{case_id}")
def get_analysis_case(case_id: str) -> dict[str, Any]:
    case = flow.get_analysis_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="analysis case not found")
    return case


@app.post("/analysis/experiments")
def create_analysis_experiment(payload: CreateExperimentPayload) -> dict[str, Any]:
    try:
        return asdict(
            flow.create_analysis_experiment(
                case_id=payload.case_id,
                name=payload.name,
                model_ids=payload.model_ids,
                agent_ids=payload.agent_ids,
                harness_ids=payload.harness_ids,
                task_ids=payload.task_ids,
                notes=payload.notes,
            )
        )
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/analysis/experiments/{experiment_id}/run")
def run_analysis_experiment(
    experiment_id: str,
    payload: RunExperimentPayload,
) -> dict[str, Any]:
    try:
        return asdict(
            flow.run_analysis_experiment(
                experiment_id,
                repo_path=payload.repo_path,
                deploy_policy=payload.deploy_policy,
            )
        )
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/analysis/experiments/{experiment_id}/batch")
def run_analysis_experiment_batch(
    experiment_id: str,
    payload: RunExperimentBatchPayload,
) -> dict[str, Any]:
    try:
        return flow.run_analysis_experiment_batch(
            experiment_id,
            repo_path=payload.repo_path,
            deploy_policy=payload.deploy_policy,
            max_concurrency=payload.max_concurrency,
        )
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/analysis/experiments/{experiment_id}/batches")
def list_analysis_experiment_batches(experiment_id: str) -> dict[str, Any]:
    return {"batches": flow.list_analysis_experiment_batches(experiment_id)}


@app.get("/analysis/batches/{batch_id}")
def get_analysis_experiment_batch(batch_id: str) -> dict[str, Any]:
    batch = flow.get_analysis_experiment_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="analysis batch not found")
    return batch


@app.get("/analysis/experiments/{experiment_id}/report")
def get_analysis_experiment_report(experiment_id: str) -> dict[str, Any]:
    try:
        return asdict(flow.experiment_report(experiment_id))
    except RequestValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/datasets/exports")
def create_dataset_export(payload: DatasetExportPayload) -> dict[str, Any]:
    return asdict(
        flow.export_slm_dataset(
            export_id=payload.export_id,
            limit=payload.limit,
            promotion_status=payload.promotion_status,
        )
    )


@app.get("/datasets/exports/{export_id}")
def get_dataset_export(export_id: str) -> dict[str, Any]:
    export = flow.get_dataset_export(export_id)
    if not export:
        raise HTTPException(status_code=404, detail="dataset export not found")
    return export


@app.post("/lab/router/recommend")
def recommend_lab_route(payload: RouterRecommendPayload) -> dict[str, Any]:
    request = JobRequest(
        prompt=payload.prompt,
        repo_path=payload.repo_path,
        repo_provider=payload.repo_provider,
        git_url=payload.git_url,
        github_repo=payload.github_repo,
        model_id=payload.model_id,
        agent_id=payload.agent_id,
        harness_id=payload.harness_id,
        deploy_policy=payload.deploy_policy,
        routing_policy=payload.routing_policy,
    )
    return asdict(flow.recommend_route(request))


@app.post("/jobs")
def create_job(payload: CreateJobPayload, background_tasks: BackgroundTasks) -> dict[str, Any]:
    request = _job_request_from_payload(payload)
    _enforce_user_quota(request)
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
    _enforce_user_quota(request)
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
    harness_id: str | None = None,
    promotion_status: PromotionStatus | None = None,
) -> dict[str, Any]:
    return {
        "runs": flow.store.list_lab_runs(
            limit=limit,
            model_id=model_id,
            agent_id=agent_id,
            harness_id=harness_id,
            promotion_status=promotion_status.value if promotion_status else None,
        )
    }


@app.get("/lab/summary")
def lab_summary() -> dict[str, Any]:
    return flow.store.lab_summary()


@app.get("/lab/leaderboard")
def lab_leaderboard(limit: int = 50) -> dict[str, Any]:
    return {"leaderboard": flow.store.lab_leaderboard(limit=limit)}


@app.get("/lab", response_class=HTMLResponse)
def lab_dashboard() -> str:
    return _lab_dashboard_html()


@app.get("/tasks/corpus")
def task_corpus() -> dict[str, Any]:
    return asdict(default_replayable_corpus())


@app.get("/users/{user_id}/quota")
def user_quota(user_id: str) -> dict[str, Any]:
    usage = flow.store.user_usage(user_id)
    quota = _user_token_quota()
    remaining = None if quota is None else max(0, quota - usage["token_budget_reserved"])
    return {
        **usage,
        "token_budget_quota": quota,
        "token_budget_remaining": remaining,
    }


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
    try:
        result = flow.run_job(job_id)
    except RequestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return asdict(result)


@app.get("/jobs/{job_id}/worker-payload")
def get_worker_payload(job_id: str) -> dict[str, Any]:
    try:
        payload = flow.build_worker_payload(job_id, status_callback_url=_worker_callback_base())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    return asdict(payload)


@app.get("/jobs/{job_id}/cloud-dispatch-plan")
def get_cloud_dispatch_plan(job_id: str) -> dict[str, Any]:
    try:
        payload = flow.build_worker_payload(job_id, status_callback_url=_worker_callback_base())
        return ecs_dispatch_planner.build_run_task_request(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/jobs/{job_id}/cloud-dispatch")
def submit_cloud_dispatch(job_id: str) -> dict[str, Any]:
    try:
        payload = flow.build_worker_payload(job_id, status_callback_url=_worker_callback_base())
        dispatch = ecs_dispatch_planner.submit_run_task(payload)
        flow.record_cloud_dispatch(dispatch)
        return asdict(dispatch)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/cloud-dispatches")
def list_cloud_dispatches(job_id: str) -> dict[str, Any]:
    if not flow.store.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"dispatches": flow.store.list_cloud_dispatches(job_id=job_id)}


@app.post("/jobs/{job_id}/worker-callback")
def post_worker_callback(job_id: str, payload: WorkerCallbackPayload) -> dict[str, Any]:
    try:
        return asdict(
            flow.record_worker_callback(
                job_id,
                callback_type=payload.callback_type,
                status=payload.status,
                payload=payload.payload,
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc


@app.get("/jobs/{job_id}/worker-callbacks")
def list_worker_callbacks(job_id: str) -> dict[str, Any]:
    if not flow.store.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"callbacks": flow.list_worker_callbacks(job_id)}


@app.get("/jobs/{job_id}/artifacts")
def list_job_artifacts(job_id: str) -> dict[str, Any]:
    if not flow.store.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"artifacts": flow.list_artifact_refs(job_id)}


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
        harness_id=parent["harness_id"],
        user_id=parent["user_id"],
        base_branch=parent["base_branch"],
        deploy_policy=DeploymentPolicy(parent["deploy_policy"]),
        routing_policy=RoutingPolicy(parent["routing_policy"]),
        token_budget=payload.token_budget or parent["token_budget"],
        max_changed_files=parent["max_changed_files"],
        max_runtime_seconds=parent["max_runtime_seconds"],
        max_prompt_chars=parent["max_prompt_chars"],
    )
    try:
        _enforce_user_quota(request)
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
        harness_id=payload.harness_id,
        user_id=payload.user_id,
        base_branch=payload.base_branch,
        deploy_policy=payload.deploy_policy,
        routing_policy=payload.routing_policy,
        token_budget=payload.token_budget,
        max_prompt_chars=payload.max_prompt_chars,
        max_runtime_seconds=payload.max_runtime_seconds,
        max_changed_files=payload.max_changed_files,
    )


def _configured_api_keys() -> set[str]:
    return {
        value.strip()
        for value in os.environ.get("AGENT_CLOUD_API_KEYS", "").split(",")
        if value.strip()
    }


def _user_token_quota() -> int | None:
    raw = os.environ.get("AGENT_CLOUD_USER_TOKEN_QUOTA", "").strip()
    if not raw:
        return None
    try:
        quota = int(raw)
    except ValueError:
        return None
    return quota if quota > 0 else None


def _worker_callback_base() -> str:
    return os.environ.get("AGENT_CLOUD_STATUS_CALLBACK_URL", "local://jobs").strip() or "local://jobs"


def _enforce_user_quota(request: JobRequest) -> None:
    quota = _user_token_quota()
    if quota is None:
        return
    usage = flow.store.user_usage(request.user_id)
    requested_total = usage["token_budget_reserved"] + request.token_budget
    if requested_total > quota:
        raise HTTPException(
            status_code=429,
            detail=(
                f"user token budget quota exceeded: requested {requested_total} "
                f"> quota {quota}"
            ),
        )


def _lab_dashboard_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Lab</title>
  <style>
    :root { color-scheme: light dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    body { margin: 0; background: #f6f7f8; color: #171717; }
    main { max-width: 1160px; margin: 0 auto; padding: 28px 20px 42px; }
    header { display: flex; justify-content: space-between; gap: 18px; align-items: end; }
    h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    h2 { margin: 28px 0 12px; font-size: 16px; letter-spacing: 0; }
    .summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .metric { border: 1px solid #d8dadd; border-radius: 8px; padding: 14px; background: #ffffff; }
    .metric strong { display: block; font-size: 26px; margin-bottom: 4px; }
    .toolbar, .inline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    button, input {
      border: 1px solid #171717;
      border-radius: 6px;
      padding: 8px 12px;
    }
    button {
      background: #171717;
      color: #ffffff;
      cursor: pointer;
    }
    input { min-width: min(100%, 360px); background: #ffffff; color: #171717; }
    .panel {
      border: 1px solid #d8dadd;
      border-radius: 8px;
      padding: 14px;
      background: #ffffff;
      margin-top: 12px;
    }
    .panel h2 { margin-top: 0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 0; font-size: 12px; }
    table {
      width: 100%;
      border-collapse: collapse;
      background: #ffffff;
      border: 1px solid #d8dadd;
      border-radius: 8px;
      overflow: hidden;
    }
    th, td {
      text-align: left;
      padding: 10px 12px;
      border-bottom: 1px solid #e5e7ea;
      font-size: 14px;
    }
    th { background: #eceff3; font-weight: 700; }
    tr:last-child td { border-bottom: 0; }
    .status { font-weight: 700; }
    .promote { color: #0b6b3a; }
    .reject { color: #a12b2b; }
    .needs_review { color: #7a4a00; }
    @media (prefers-color-scheme: dark) {
      body { background: #141414; color: #f4f4f5; }
      .metric, .panel, table { background: #1e1e1d; border-color: #3b3833; }
      th { background: #2a2824; }
      th, td { border-color: #34312c; }
      button { border-color: #f4f4f5; background: #f4f4f5; color: #141414; }
      input { background: #141414; color: #f4f4f5; border-color: #f4f4f5; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Agent Lab</h1>
      </div>
      <div class="toolbar">
        <button type="button" id="refresh">Refresh</button>
      </div>
    </header>
    <section class="summary" id="summary"></section>
    <section class="grid">
      <div class="panel">
        <h2>Cloud Worker</h2>
        <pre id="cloud"></pre>
      </div>
      <div class="panel">
        <h2>Router Recommendation</h2>
        <div class="inline">
          <input id="routerPrompt" value="For my shopping website, create a buy button.">
          <button type="button" id="recommend">Recommend</button>
        </div>
        <pre id="route"></pre>
      </div>
      <div class="panel">
        <h2>Dataset Export</h2>
        <button type="button" id="exportDataset">Export</button>
        <pre id="dataset"></pre>
      </div>
    </section>
    <section>
      <h2>Analysis Cases</h2>
      <table>
        <thead>
          <tr>
            <th>Case</th>
            <th>Category</th>
            <th>Models</th>
            <th>Harnesses</th>
          </tr>
        </thead>
        <tbody id="cases"></tbody>
      </table>
    </section>
    <section>
      <h2>Leaderboard</h2>
      <table>
        <thead>
          <tr>
            <th>Model</th>
            <th>Agent</th>
            <th>Harness</th>
            <th>Runs</th>
            <th>Promote</th>
            <th>Needs Review</th>
            <th>Reject</th>
            <th>Rate</th>
          </tr>
        </thead>
        <tbody id="leaderboard"></tbody>
      </table>
    </section>
    <section>
      <h2>Recent Runs</h2>
      <table>
        <thead>
          <tr>
            <th>Job</th>
            <th>User</th>
            <th>Model</th>
            <th>Agent</th>
            <th>Harness</th>
            <th>Promotion</th>
            <th>Changed</th>
            <th>Tokens</th>
          </tr>
        </thead>
        <tbody id="runs"></tbody>
      </table>
    </section>
  </main>
  <script>
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char]));
    }

    async function loadLab() {
      const [summary, leaderboard, runs] = await Promise.all([
        fetch('/lab/summary').then((response) => response.json()),
        fetch('/lab/leaderboard').then((response) => response.json()),
        fetch('/lab/runs?limit=50').then((response) => response.json())
      ]);
      const [cloud, cases] = await Promise.all([
        fetch('/integrations/cloud/status').then((response) => response.json()),
        fetch('/analysis/cases').then((response) => response.json())
      ]);
      const statuses = summary.by_promotion_status || {};
      document.getElementById('summary').innerHTML = [
        ['Total', summary.total_runs || 0],
        ['Promote', statuses.promote || 0],
        ['Needs Review', statuses.needs_review || 0],
        ['Reject', statuses.reject || 0]
      ].map(([label, value]) =>
        `<div class="metric"><strong>${escapeHtml(value)}</strong>${escapeHtml(label)}</div>`
      ).join('');
      document.getElementById('cloud').textContent = JSON.stringify(cloud, null, 2);
      document.getElementById('cases').innerHTML = (cases.cases || []).map((row) => `
        <tr>
          <td>${escapeHtml(row.case_id)}</td>
          <td>${escapeHtml(row.category)}</td>
          <td>${escapeHtml((row.model_ids || []).join(', '))}</td>
          <td>${escapeHtml((row.harness_ids || []).join(', '))}</td>
        </tr>
      `).join('');
      document.getElementById('leaderboard').innerHTML =
        (leaderboard.leaderboard || []).map((row) => `
        <tr>
          <td>${escapeHtml(row.model_id)}</td>
          <td>${escapeHtml(row.agent_id)}</td>
          <td>${escapeHtml(row.harness_id)}</td>
          <td>${escapeHtml(row.total_runs)}</td>
          <td>${escapeHtml(row.promote_count)}</td>
          <td>${escapeHtml(row.needs_review_count)}</td>
          <td>${escapeHtml(row.reject_count)}</td>
          <td>${escapeHtml(Math.round((row.promotion_rate || 0) * 100))}%</td>
        </tr>
      `).join('');
      document.getElementById('runs').innerHTML = (runs.runs || []).map((run) => `
        <tr>
          <td>${escapeHtml(run.job_id)}</td>
          <td>${escapeHtml(run.user_id)}</td>
          <td>${escapeHtml(run.model_id)}</td>
          <td>${escapeHtml(run.agent_id)}</td>
          <td>${escapeHtml(run.harness_id)}</td>
          <td class="status ${escapeHtml(run.promotion_status)}">
            ${escapeHtml(run.promotion_status)}
          </td>
          <td>${escapeHtml(run.changed_files_count)}</td>
          <td>${escapeHtml(run.tokens_used)}/${escapeHtml(run.token_budget)}</td>
        </tr>
      `).join('');
    }
    document.getElementById('refresh').addEventListener('click', loadLab);
    document.getElementById('recommend').addEventListener('click', async () => {
      const response = await fetch('/lab/router/recommend', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          prompt: document.getElementById('routerPrompt').value,
          routing_policy: 'recommend_only'
        })
      });
      document.getElementById('route').textContent =
        JSON.stringify(await response.json(), null, 2);
    });
    document.getElementById('exportDataset').addEventListener('click', async () => {
      const response = await fetch('/datasets/exports', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ limit: 50 })
      });
      document.getElementById('dataset').textContent =
        JSON.stringify(await response.json(), null, 2);
    });
    loadLab();
  </script>
</body>
</html>"""


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
