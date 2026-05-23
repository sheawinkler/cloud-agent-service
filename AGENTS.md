# Agent Instructions for `cloud_agent_service`

This directory is a local-only MVP for a cloud coding-agent platform. It mirrors
the intended AWS/ECS architecture without creating cloud resources, pushing to
GitHub, or deploying real infrastructure.

## Boundaries

- Keep work local unless the user explicitly asks for real cloud/GitHub actions.
- Treat `local://github/pr/<job_id>` as a mock PR URL, not a real GitHub PR.
- Treat `deployed: local mock deployment recorded` as a local artifact, not a
  production deployment.
- Do not persist secrets in docs, logs, SQLite data, test fixtures, or examples.
- Do not commit or preserve runtime artifacts from `.runtime/`.
- Docker Compose runtime state lives in the `runtime_data` Docker volume.

## Main Files

- `app.py`: FastAPI surface for job creation, status, and health.
- `pipeline.py`: request validation, prompt upgrade, planning, local workspace
  copy, deterministic coding action, tests, gates, mock PR sync, and mock deploy.
- `store.py`: SQLite job and event persistence.
- `orchestrator.py`: local queue and one-job runner.
- `worker.py`: container-friendly one-job entry point.
- `compose.yaml`: local Docker Compose wiring.
- `scripts/install_allowed_modules.sh`: dependency allowlist installer.
- `scripts/demo_local_flow.py`: standard-library demo path for the happy flow.
- `EVALUATION.md`: criteria for judging product and operational readiness.
- `examples/agent_contract.json`: example job payload and final result contract.

## Run

Compile and smoke-check the Python code:

```bash
python3 -m compileall cloud_agent_service scripts tests
python3 scripts/demo_local_flow.py
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

Monitor the API:

```bash
docker --context orbstack compose -f compose.yaml ps
docker --context orbstack compose -f compose.yaml logs --tail=120 api
curl -sS http://127.0.0.1:8000/jobs/<job_id>
```

Stop the local stack:

```bash
docker --context orbstack compose -f compose.yaml down
```

## Expected Job Events

A successful local run should emit these core events:

1. `job_created`
2. `job_queued`
3. `agent_dispatched`
4. `repo_cloned`
5. `prompt_upgraded`
6. `plan_created`
7. `dependencies_requested`
8. `files_changed`
9. `tests_finished`
10. `policy_gate_result`
11. `branch_pushed`
12. `pr_created_or_updated`
13. `deployment_finished`
14. `job_succeeded`

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
