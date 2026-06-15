# Agent Instructions for `cloud_agent_service`

This directory is an MVP for a cloud coding-agent platform. It mirrors the
intended AWS/ECS architecture and can submit ECS tasks only when explicit live
submit env flags are configured. Local repo jobs use mock PR/deploy artifacts. Generic Git jobs
clone and push a review branch to `origin`. GitHub repo jobs use the real GitHub
App clone, branch push, and PR path only when app credentials are configured.
Each repo update is also a minimal lab run: `ModelSpec` + `AgentSpec` +
`HarnessSpec` + evidence -> `PromotionDecision`.
DuckDB is the lab warehouse when available; SQLite remains the default
operational job store. Vercel preview deployment and non-local execution
providers are opt-in contracts unless their explicit env flags and credentials
are configured.

## Boundaries

- Keep work local unless the user explicitly asks for real cloud/GitHub actions.
- Treat `local://github/pr/<job_id>` as a mock PR URL, not a real GitHub PR.
- Treat `repo_provider=git` as provider-agnostic Git: clone `git_url`, push an
  agent branch, and return a review ref rather than a provider-native PR.
- Treat `repo_provider=github` as a real GitHub App path; require
  `/integrations/github/status` to report configured before claiming it is live.
- Treat `local://preview/<job_id>/<file>` as a local preview artifact, not a
  hosted internet URL.
- Treat `local-deterministic` as the current deterministic model spec; do not
  claim live external SLM/LLM inference unless an actual provider call is wired
  and verified.
- Treat `harness_id` as the selected runtime contract. `local-template` is the
  default deterministic harness. Ranked and `custom:<name>` harness IDs are
  indexed for dispatch routing, but do not execute arbitrary third-party CLIs
  unless the worker image or adapter actually implements that harness.
- Treat `deployed: local mock deployment recorded` as a local artifact, not a
  production deployment.
- Treat `/jobs/<job_id>/cloud-dispatch-plan` as dry-run. Treat
  `/jobs/<job_id>/cloud-dispatch` as live only when
  `AGENT_CLOUD_ECS_SUBMIT_ENABLED=1` and AWS/ECS env is configured.
- Set `AGENT_CLOUD_STATUS_CALLBACK_URL` to the externally reachable API `/jobs`
  base before live cloud dispatch; local defaults intentionally skip callbacks.
- Treat artifact storage as local unless `AGENT_CLOUD_ARTIFACT_PROVIDER` and
  related env are configured.
- Treat SQLite as the default operational job store. Treat DuckDB as the lab
  warehouse/read model unless `AGENT_CLOUD_DB_PROVIDER=duckdb` is explicitly set
  for local operational-store testing. Do not present DuckDB as production
  multi-writer infrastructure.
- Treat Postgres/RDS as the production database target contract until a real SQL
  adapter is implemented and verified.
- Treat worker leases as the control-plane truth for active workers; callbacks
  are supporting evidence and heartbeat input.
- Treat Vercel preview deployment as a recorded contract unless
  `AGENT_CLOUD_DEPLOYMENT_PROVIDER=vercel_preview`,
  `AGENT_CLOUD_VERCEL_DEPLOY_ENABLED=1`, and `VERCEL_TOKEN` are configured.
- Treat Vercel Sandbox execution as a status/contract until a live sandbox
  adapter is implemented and verified.
- Do not persist secrets in docs, logs, SQLite data, test fixtures, or examples.
- Do not commit or preserve runtime artifacts from `.runtime/`.
- Docker Compose runtime state lives in the `runtime_data` Docker volume.

## Main Files

- `app.py`: FastAPI surface for job creation, status, and health.
- `pipeline.py`: request validation, prompt upgrade, planning, local workspace
  copy, generic Git clone/sync, GitHub App clone/sync, repo profiling and
  memory, model/agent registry, budget charging, deterministic coding action,
  tests, gates, preview proof, promotion decision, mock PR sync, and mock deploy.
- `store.py`: operational job, event, callback, lease, and lab-run persistence.
- `database.py`: SQLite/DuckDB embedded adapter plus Postgres target readiness.
- `lab_warehouse.py`: DuckDB materialized lab read model.
- `orchestrator.py`: local queue plus persisted queued-job runner.
- `worker.py`: container-friendly one-job or claim-next entry point.
- `cloud_dispatch.py`: AWS ECS/Fargate dry-run dispatch request builder and
  env-gated live submitter.
- `deployment.py`: local mock and Vercel preview deployment provider contracts.
- `execution.py`: local/ECS/Vercel Sandbox execution-provider status contracts.
- `provenance.py`: successful-run provenance manifest writer.
- `artifact_store.py`: local/S3 artifact-reference indexing.
- `harness_registry.py`: pre-indexed agent harness registry, top-20 slice, and
  custom harness contract support.
- `harness_adapters.py`: harness adapter ABI, deterministic local adapter, and
  opt-in Pi coding-agent CLI adapter.
- `security_profiles.py`: per-harness command, secret, path, network, and
  runtime security contracts.
- `artifact_schema.py`: replayable run artifact writer for transcript, diff,
  and artifact policy evidence.
- `task_corpus.py`: shared replayable task corpus for API and evaluator.
- `analysis_lab.py`: seeded analysis cases, experiment analysis, and report
  aggregation.
- `dataset_export.py`: redacted SLM JSONL export from replay artifacts.
- `lab_router.py`: leaderboard-backed model/agent/harness routing decisions.
- `compose.yaml`: local Docker Compose wiring.
- `scripts/install_allowed_modules.sh`: dependency allowlist installer.
- `demo.sh`: one-command local demo.
- `scripts/demo_local_flow.py`: standard-library demo path for the happy flow.
- `scripts/evaluate_mvp.py`: golden buy-button task evaluator.
- `scripts/evaluate_task_suite.py`: multi-run lab task-suite evaluator.
- `scripts/smoke_api.py`: standard-library smoke suite for the live API.
- `EVALUATION.md`: criteria for judging product and operational readiness.
- `examples/agent_contract.json`: example job payload and final result contract.
  Terminal jobs are indexed in `lab_runs` for model/agent promotion summaries.

## Run

Compile and smoke-check the Python code:

```bash
python3 -m compileall cloud_agent_service scripts tests
./demo.sh
python3 scripts/evaluate_mvp.py
python3 -m unittest tests.test_cloud_agent_service_flow
python3 -m unittest discover -s tests
```

Build and run the API container:

```bash
docker --context orbstack compose -f compose.yaml up -d --build api
curl -sS http://127.0.0.1:8000/health
```

Submit a local job against the mounted repository:

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "For my shopping website, create a buy button.",
    "repo_path": "/host_repo",
    "deploy_policy": "manual"
  }'
```

Submit and run a job in one API call:

```bash
curl -X POST http://127.0.0.1:8000/run-code-job \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "For my shopping website, create a buy button.",
    "repo_path": "/host_repo",
    "deploy_policy": "preview_only"
  }'
```

Monitor the API:

```bash
docker --context orbstack compose -f compose.yaml ps
docker --context orbstack compose -f compose.yaml logs --tail=120 api
curl -sS http://127.0.0.1:8000/jobs/<job_id>
curl -sS http://127.0.0.1:8000/jobs/<job_id>/worker-payload
curl -sS http://127.0.0.1:8000/jobs/<job_id>/artifacts
curl -sS http://127.0.0.1:8000/jobs/<job_id>/worker-callbacks
curl -sS http://127.0.0.1:8000/jobs/<job_id>/provenance
curl -sS http://127.0.0.1:8000/jobs/<job_id>/budget
curl -sS http://127.0.0.1:8000/jobs/<job_id>/events
curl -sS http://127.0.0.1:8000/lab/runs
curl -sS http://127.0.0.1:8000/lab/summary
curl -sS http://127.0.0.1:8000/lab/leaderboard
curl -sS http://127.0.0.1:8000/tasks/corpus
curl -sS http://127.0.0.1:8000/analysis/cases
curl -sS http://127.0.0.1:8000/datasets/exports/<export_id>
curl -sS http://127.0.0.1:8000/models
curl -sS http://127.0.0.1:8000/harnesses
curl -sS http://127.0.0.1:8000/auth/status
curl -sS http://127.0.0.1:8000/integrations/cloud/status
curl -sS http://127.0.0.1:8000/integrations/database/status
curl -sS http://127.0.0.1:8000/integrations/deploy/status
curl -sS http://127.0.0.1:8000/integrations/execution/status
curl -sS http://127.0.0.1:8000/jobs/<job_id>/cloud-dispatch-plan
python -m cloud_agent_service.worker --claim-next
```

Stop the local stack:

```bash
docker --context orbstack compose -f compose.yaml down
```

## Expected Job Events

A successful local run should emit these core events:

1. `job_created`
2. `routing_decision_created`
3. `harness_selected`
4. `job_queued`
5. `budget_charged`
6. `agent_dispatched`
7. `lab_run_configured`
8. `harness_adapter_selected`
9. `harness_security_profile_selected`
10. `repo_cloned`
11. `repo_analyzed`
12. `repo_memory_loaded`
13. `prompt_upgraded`
14. `plan_created`
15. `dependencies_requested`
16. `harness_adapter_finished`
17. `files_changed`
18. `tests_finished`
19. `preview_created`
20. `browser_proof_finished`
21. `run_artifact_created`
22. `policy_gate_result`
23. `branch_pushed`
24. `pr_created_or_updated`
25. `deployment_finished`
26. `job_succeeded`
27. `promotion_decision_created`

If a gate fails, the job must stop before mock PR sync or mock deployment.

## Development Rules

- Hold Python changes to PEP 8 standards. Use the repo Ruff config for import
  ordering, formatting, and lint checks before claiming quality.
- Engineer for performance-oriented solutions: keep hot paths simple, avoid
  avoidable disk scans and repeated subprocess work, preserve bounded runtime
  and memory behavior, and choose streaming or incremental processing when job
  artifacts can grow.
- Prefer standard-library changes in `pipeline.py` and `store.py` unless the API
  layer truly needs a dependency.
- Keep tests deterministic and filesystem-isolated with temporary directories.
- Run at least `python3 -m unittest tests.test_cloud_agent_service_flow` after changes.
- Run the full `python3 -m unittest discover -s tests` before claiming repo-wide
  verification.
- Rebuild Docker images after changing `cloud_agent_service/*.py`,
  `requirements.txt`, or either Dockerfile.
- Keep docs explicit about local/mock behavior.
