# Agent Instructions for `cloud_agent_service`

This directory is an MVP for a cloud coding-agent platform. It mirrors the
intended AWS/ECS architecture without creating cloud resources or deploying real
infrastructure. Local repo jobs use mock PR/deploy artifacts. Generic Git jobs
clone and push a review branch to `origin`. GitHub repo jobs use the real GitHub
App clone, branch push, and PR path only when app credentials are configured.
Each repo update is also a minimal lab run: `ModelSpec` + `AgentSpec` +
`HarnessSpec` + evidence -> `PromotionDecision`.

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
- Do not persist secrets in docs, logs, SQLite data, test fixtures, or examples.
- Do not commit or preserve runtime artifacts from `.runtime/`.
- Docker Compose runtime state lives in the `runtime_data` Docker volume.

## Main Files

- `app.py`: FastAPI surface for job creation, status, and health.
- `pipeline.py`: request validation, prompt upgrade, planning, local workspace
  copy, generic Git clone/sync, GitHub App clone/sync, repo profiling and
  memory, model/agent registry, budget charging, deterministic coding action,
  tests, gates, preview proof, promotion decision, mock PR sync, and mock deploy.
- `store.py`: SQLite job and event persistence.
- `orchestrator.py`: local queue plus persisted queued-job runner.
- `worker.py`: container-friendly one-job or claim-next entry point.
- `cloud_dispatch.py`: AWS ECS/Fargate dry-run dispatch request builder.
- `harness_registry.py`: pre-indexed agent harness registry, top-20 slice, and
  custom harness contract support.
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
curl -sS http://127.0.0.1:8000/jobs/<job_id>/budget
curl -sS http://127.0.0.1:8000/jobs/<job_id>/events
curl -sS http://127.0.0.1:8000/lab/runs
curl -sS http://127.0.0.1:8000/lab/summary
curl -sS http://127.0.0.1:8000/models
curl -sS http://127.0.0.1:8000/harnesses
curl -sS http://127.0.0.1:8000/auth/status
curl -sS http://127.0.0.1:8000/integrations/cloud/status
python -m cloud_agent_service.worker --claim-next
```

Stop the local stack:

```bash
docker --context orbstack compose -f compose.yaml down
```

## Expected Job Events

A successful local run should emit these core events:

1. `job_created`
2. `harness_selected`
3. `job_queued`
4. `agent_dispatched`
5. `budget_charged`
6. `repo_cloned`
7. `repo_analyzed`
8. `repo_memory_loaded`
9. `lab_run_configured`
10. `prompt_upgraded`
11. `plan_created`
12. `dependencies_requested`
13. `files_changed`
14. `tests_finished`
15. `policy_gate_result`
16. `preview_created`
17. `browser_proof_finished`
18. `branch_pushed`
19. `pr_created_or_updated`
20. `deployment_finished`
21. `job_succeeded`
22. `promotion_decision_created`

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
