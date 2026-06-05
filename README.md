# Cloud Agent Service MVP

`cloud_agent_service` is an MVP implementation of the planned cloud coding-agent
platform. It proves the application flow with deterministic local defaults:
API intake, prompt validation, prompt upgrade, job state, queue/orchestration,
agent execution, tests, policy gates, preview proof, PR sync, and deployment
policy handling.
It also includes the cloud-ready operational boundaries needed before real
AWS/GitHub rollout: durable queue claiming, worker payloads, budget ledger,
event streaming, repo profiling, repo memory, approval gates, continuation, and
evaluation.

Local repo jobs still use mock PR and deployment artifacts. GitHub repo jobs use
a GitHub App path when credentials are configured: clone by installation token,
push an agent branch, and create or reuse a pull request. The MVP does not
create AWS resources or perform production deployment.

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
| 7. Copy/clone workspace |
| 8. Analyze repo         |
| 9. Apply agent edit     |
| 10. Run tests + gates   |
| 11. Preview + proof     |
| 12. PR sync + deploy    |
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
  GitHub App clone/sync, deterministic edit, tests, policy gates, preview
  artifacts, local GitHub sync mock, and local deployment mock.
- `store.py`: SQLite job and event persistence.
- `orchestrator.py`: local in-memory queue plus persisted queued-job runner.
- `worker.py`: container-friendly single-job or claim-next entry point.
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
7. Copy or clone the target repo into an isolated workspace.
8. Analyze repo framework, package manager, and test hints.
9. Execute a deterministic local coding action.
10. Run tests and policy gates before sync/deploy.
11. Track budget usage before each major stage.
12. Publish a local preview artifact and browser-proof checks.
13. Return final status with events, changed files, checks, evidence, PR URL,
    and deployment status.

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
- `preview_created` and `browser_proof_finished` in the event list

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

List recent jobs:

```bash
curl -sS http://127.0.0.1:8000/jobs
```

Inspect the worker payload that would be handed to an ECS/Fargate task:

```bash
curl -sS http://127.0.0.1:8000/jobs/<job_id>/worker-payload
```

Run the next persisted queued job without relying on the API process memory:

```bash
python -m cloud_agent_service.worker --claim-next
```

Read budget ledger:

```bash
curl -sS http://127.0.0.1:8000/jobs/<job_id>/budget
```

Read or stream job events:

```bash
curl -sS http://127.0.0.1:8000/jobs/<job_id>/events
curl -N http://127.0.0.1:8000/jobs/<job_id>/events/stream
```

Cancel a queued job before dispatch:

```bash
curl -X POST http://127.0.0.1:8000/jobs/<job_id>/cancel
```

Retry a failed or cancelled job:

```bash
curl -X POST http://127.0.0.1:8000/jobs/<job_id>/retry
```

Approve a manual deployment after job success:

```bash
curl -X POST http://127.0.0.1:8000/jobs/<job_id>/approve-deployment
```

Check whether real GitHub App credentials are configured:

```bash
curl -sS http://127.0.0.1:8000/integrations/github/status
```

## GitHub App Jobs

Set these environment variables in the API/worker runtime to enable real GitHub
App sync:

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_INSTALLATION_ID=987654
export GITHUB_APP_PRIVATE_KEY="$(cat /path/to/private-key.pem)"
```

Optional:

```bash
export GITHUB_API_URL=https://api.github.com
```

Submit a GitHub-backed job:

```bash
curl -X POST http://127.0.0.1:8000/run-code-job \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "For my shopping website, create a buy button.",
    "repo_provider": "github",
    "github_repo": "owner/repo",
    "base_branch": "main",
    "deploy_policy": "pr_only"
  }'
```

Continue from a prior job while preserving provider, target repo, base branch,
and working branch lineage:

```bash
curl -X POST http://127.0.0.1:8000/jobs/<job_id>/continue \
  -H 'content-type: application/json' \
  -d '{"prompt": "Make the buy button more prominent."}'
```

GitHub App jobs are only live when `/integrations/github/status` reports
`configured: true`. Without those credentials, the status endpoint is a readiness
check, not proof of a successful GitHub clone, push, or PR.

## Deployment Policies

- `manual`: job succeeds, deployment waits for `/approve-deployment`.
- `local`: write local mock deployment artifact after successful gates.
- `never`: skip deployment.
- `pr_only`: sync PR, skip deployment.
- `preview_only`: publish preview/proof, skip deployment.
- `staging_auto`: write local staging mock deployment artifact.
- `production_approval`: job succeeds, deployment waits for approval.

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
- `budget_ledger`: token/runtime accounting table inside SQLite.
- `repo_memory`: per-repo last-run profile and summary inside SQLite.
- `workspaces/<job_id>/repo`: isolated copied repo workspace.
- `artifacts/<job_id>-pr.json`: mock PR payload.
- `artifacts/<job_id>-deployment.json`: mock deployment payload.
- `artifacts/previews/<job_id>/index.html`: local preview copy when HTML exists.
- `artifacts/previews/<job_id>/browser-proof.json`: browser-proof checks.

## Policy Gates

A job must pass all gates before mock PR sync and mock deployment:

- repo tests pass
- secret scan passes
- diff size policy passes
- protected path policy passes
- dependency policy passes
- deployment policy passes

If a gate fails, the job stops and reports `failed`.

Protected paths include secrets, GitHub workflows, Docker/Compose files, and
Terraform files. Dependency manifest/lockfile changes are blocked in this local
MVP unless that policy is relaxed in code.

## Evaluation Harness

Run the golden buy-button task and emit a score:

```bash
python3 scripts/evaluate_mvp.py
```

The evaluator checks job success, visible button insertion, tests, policy gates,
mock PR artifact, mock deployment artifact, preview artifact, and browser-proof
checks.

Run the live API smoke suite after starting Docker Compose:

```bash
docker --context orbstack compose -f compose.yaml up -d --build api
python3 scripts/smoke_api.py --base-url http://127.0.0.1:8000 --repo-path /host_repo
```

The API smoke covers health, GitHub integration status, worker payload, job run,
budget ledger, event list, SSE stream, manual deployment approval, one-click
run, continuation, and budget-stop failure.

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

The MVP is still cloud-ready rather than fully cloud-native:

- local repo copy by default; GitHub App clone/sync only when credentials exist
- SQLite queued-job claim instead of SQS
- local Docker/worker contract instead of ECS/Fargate
- local SQLite instead of managed Postgres/DynamoDB
- local mock PR artifact for local jobs; real GitHub PR path for GitHub jobs
- local mock deployment artifact instead of AWS deploy

That keeps the full flow testable before replacing each local component with a
cloud-backed implementation.
