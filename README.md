# Cloud Agent Service Local MVP

`cloud_agent_service` is a local-only implementation of the planned cloud coding-agent
platform. It proves the application flow with deterministic local components:
API intake, prompt validation, prompt upgrade, job state, queue/orchestration,
agent execution, tests, policy gates, mock GitHub sync, and mock deployment.

It intentionally does not create AWS resources, push to GitHub, or perform a
real deployment.

## App Flow

```text
User Request
    |
    v
+-------------------------+
| Cloud Agent Service     |
+-------------------------+
| 1. API intake           |
| 2. Validate request     |
| 3. Upgrade prompt       |
| 4. Create job record    |
| 5. Queue job            |
| 6. Dispatch worker      |
| 7. Copy workspace       |
| 8. Apply agent edit     |
| 9. Run tests + gates    |
| 10. Mock PR + deploy    |
+-------------------------+
    |
    v
Final Job Result

Failure exits:
  - Invalid request  -> fail before dispatch
  - Test/gate failure -> no PR, no deploy

Monitoring:
  - SQLite job events
  - Docker logs
  - GET /jobs/{job_id}
```

## Components

- `app.py`: FastAPI surface for job creation, status, and health checks.
- `pipeline.py`: request validation, prompt upgrade, planning, local repo copy,
  deterministic edit, tests, policy gates, local GitHub sync mock, and local
  deployment mock.
- `store.py`: SQLite job and event persistence.
- `orchestrator.py`: local queue and one-job runner.
- `worker.py`: container-friendly single-job entry point.
- `Dockerfile.api`: API container.
- `Dockerfile.agent`: worker container.
- `compose.yaml`: local API/worker build configuration.
- `AGENTS.md`: operating instructions for coding agents.
- `EVALUATION.md`: criteria for judging product and operational readiness.
- `examples/agent_contract.json`: example worker payload and final result shape.
- `demo.sh`: one-command local demo.
- `scripts/demo_local_flow.py`: no-cloud, no-Docker proof path.
- `llm.txt`: compact orientation file for LLM agents.

## What The MVP Proves

1. Receive a user prompt.
2. Reject invalid input before dispatch.
3. Normalize the prompt into a concise implementation brief.
4. Create a durable job record.
5. Queue and dispatch the job.
6. Run the job through a container-compatible worker contract.
7. Copy the target repo into an isolated workspace.
8. Execute a deterministic local coding action.
9. Run tests and policy gates before sync/deploy.
10. Return final status with events, changed files, checks, mock PR URL, and
    mock deployment status.

## Simple Demo

Run a complete local job without Docker or external services:

```bash
./demo.sh
```

The demo creates a temporary shopping-site repo, submits the buy-button request,
runs the full service pipeline, and prints a short proof summary. Look for:

- `status: succeeded`
- `changed_files: index.html`
- `tests_failed: 0`
- `job_succeeded` in the event list

For the full payload:

```bash
./demo.sh --json
```

## Local Run

Compile and test:

```bash
python3 -m compileall cloud_agent_service scripts tests
./demo.sh
python3 -m unittest tests.test_cloud_agent_service_flow
python3 -m unittest discover -s tests
```

Run the API with Docker:

```bash
docker --context orbstack compose -f compose.yaml up -d --build api
curl -sS http://127.0.0.1:8000/health
```

Run the API directly, after installing dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn cloud_agent_service.app:app --reload
```

Stop the Docker stack:

```bash
docker --context orbstack compose -f compose.yaml down
```

## Submit A Job

Against Docker Compose, the host repo is mounted as `/host_repo`:

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "For my shopping website, create a buy button.",
    "repo_path": "/host_repo",
    "deploy_policy": "manual"
  }'
```

Fetch status:

```bash
curl -sS http://127.0.0.1:8000/jobs/<job_id>
```

Monitor containers:

```bash
docker --context orbstack compose -f compose.yaml ps
docker --context orbstack compose -f compose.yaml logs --tail=120 api
```

## Runtime Data

Docker Compose stores runtime data in the `runtime_data` Docker volume. Direct
local execution writes runtime data under `.runtime/`, which is ignored and
should not be committed.

Artifacts include:

- `jobs.sqlite3`: job and event state.
- `workspaces/<job_id>/repo`: isolated copied repo workspace.
- `artifacts/<job_id>-pr.json`: mock PR payload.
- `artifacts/<job_id>-deployment.json`: mock deployment payload.

## Policy Gates

A job must pass all gates before mock PR sync and mock deployment:

- repo tests pass
- secret scan passes
- diff size policy passes
- dependency policy passes
- deployment policy passes

If a gate fails, the job stops and reports `failed`.

## Evaluation And Contracts

- `EVALUATION.md`: how to judge the service as an agent control loop.
- `examples/agent_contract.json`: example worker payload and final result shape.
- `scripts/demo_local_flow.py`: no-cloud, no-Docker proof path for the happy flow.

## Tooling Research Notes

No tool below is wired into the MVP yet.

### Repomix

Repomix looks useful as an optional context-pack step. Its README describes it
as a tool that packs a repository into an AI-friendly file, supports token
counting, respects ignore files, can use `.repomixignore`, and includes secret
scanning. That maps well to a future "repo context snapshot" stage before prompt
upgrade.

Recommended use: optional, per-job context pack artifact.

Do not make it required yet. It adds a Node/npm toolchain and another policy
surface. For the current local MVP, repo inspection plus scoped files are simpler
and easier to audit.

## Current Boundary

The MVP is deliberately local:

- local repo copy instead of GitHub clone
- local queue instead of SQS
- local Docker contract instead of ECS/Fargate
- local SQLite instead of managed Postgres/DynamoDB
- local mock PR artifact instead of GitHub PR
- local mock deployment artifact instead of AWS deploy

That keeps the full flow testable before replacing each local component with a
cloud-backed implementation.
