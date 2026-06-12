# Cloud Agent Service MVP

`cloud_agent_service` is an MVP implementation of the planned cloud coding-agent
platform. It proves the application flow with deterministic local defaults:
API intake, prompt validation, prompt upgrade, job state, queue/orchestration,
agent execution, tests, policy gates, preview proof, PR sync, and deployment
policy handling.
It also includes the cloud-ready operational boundaries needed before real
AWS/Git rollout: durable queue claiming, worker payloads, budget ledger, event
streaming, repo profiling, repo memory, model/agent run metadata, approval
gates, continuation, lab-run summaries, task-suite evaluation, optional
API-key/usage controls, a ranked harness index with a top-20 slice, and an ECS
dry-run dispatch contract.

Local repo jobs still use mock PR and deployment artifacts. Generic Git jobs
clone from `git_url` and push an agent branch back to `origin`. GitHub repo jobs
are a specialization that use a GitHub App installation token and create or
reuse a pull request. The MVP does not create AWS resources or perform
production deployment.

The Mirendil-facing framing is that a repo update is also a minimal Language
Model Lab run: a `ModelSpec` plus `AgentSpec` plus `HarnessSpec` executes a
bounded task and produces a `PromotionDecision` from tests, policy gates,
preview proof, and deployment policy. This is not a training or fine-tuning
system.

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
  generic Git clone/sync, GitHub App clone/PR sync, deterministic edit, tests,
  policy gates, preview artifacts, local GitHub sync mock, and local deployment
  mock.
- `store.py`: SQLite job and event persistence.
- `orchestrator.py`: local in-memory queue plus persisted queued-job runner.
- `worker.py`: container-friendly single-job or claim-next entry point.
- `cloud_dispatch.py`: AWS ECS/Fargate dry-run dispatch request builder.
- `harness_registry.py`: curated agent harness index, top-20 slice, and custom
  harness contract support.
- `Dockerfile.api`: API container.
- `Dockerfile.agent`: worker container.
- `compose.yaml`: local API/worker build configuration.
- `AGENTS.md`: operating instructions for coding agents.
- `EVALUATION.md`: criteria for judging product and operational readiness.
- `examples/agent_contract.json`: example worker payload and final result shape.
- `demo.sh`: one-command local demo.
- `scripts/demo_local_flow.py`: no-cloud, no-Docker proof path.
- `scripts/evaluate_task_suite.py`: multi-run task-suite evaluator.
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
13. Record the model/agent/harness config that produced the change.
14. Return final status with events, changed files, checks, evidence, PR URL,
    deployment status, and promotion decision.
15. Index terminal runs for lab history and model/agent promotion summaries.
16. Expose model/runtime status, harness status, user quota usage, and cloud
    dispatch contracts.

## Model And Agent Lab Layer

Every job carries a small lab contract:

- `ModelSpec`: provider, model name, context window, cost tier, tool support.
- `AgentSpec`: role, bound model, allowed commands, output contract.
- `HarnessSpec`: selected runtime harness, execution contract, install hint,
  source URLs, env requirements, and integration notes.
- `PromotionDecision`: `promote`, `reject`, or `needs_review` with evidence.

The default `local-deterministic` model and `repo-editor-v1` agent are explicit
so the deterministic MVP can be compared against future external SLM/LLM-backed
agents without changing the repo dispatch contract.

Terminal jobs are also written to a `lab_runs` index. This makes promotion
outcomes queryable by model, agent, harness, and status instead of burying them
inside individual job payloads.

An OpenAI Responses-backed model path is available through the
`gpt-5-coding` model and `openai-repo-editor-v1` agent. It is disabled by
default and requires both `AGENT_CLOUD_ENABLE_OPENAI_AGENT=1` and
`OPENAI_API_KEY`; otherwise the run fails as a configuration error instead of
silently falling back to the deterministic model.

## Agent Harness Index

`GET /harnesses` exposes a curated harness registry for cloud repo-editing
workers plus a `top_20` slice for the current strongest defaults. The index
covers terminal coding agents, long-running harnesses, managed cloud coding
agents, and production agent SDKs. It is intentionally a dispatch contract: the
service records `harness_id`, includes `harness_spec` in the worker payload and
final evidence, and passes `AGENT_CLOUD_HARNESS_ID` into ECS dry-run plans. It
does not execute arbitrary third-party CLIs unless the worker image or adapter
has been built for that harness.

Use `local-template` for the deterministic local harness, one of the indexed
harness IDs such as `factory-droid`, `pi-coding-agent`, `hermes-agent`,
`openhands`, or `openai-codex-cli`, or a custom safe ID like
`custom:internal-runner`.

```bash
curl -sS http://127.0.0.1:8000/harnesses
curl -sS http://127.0.0.1:8000/harnesses/factory-droid
curl -sS 'http://127.0.0.1:8000/lab/runs?harness_id=local-template'
```

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
python3 scripts/evaluate_mvp.py
python3 scripts/evaluate_task_suite.py
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

List lab runs and summarize promotion outcomes:

```bash
curl -sS http://127.0.0.1:8000/lab/runs
curl -sS 'http://127.0.0.1:8000/lab/runs?model_id=local-deterministic&promotion_status=promote'
curl -sS http://127.0.0.1:8000/lab/summary
open http://127.0.0.1:8000/lab
curl -sS http://127.0.0.1:8000/models
curl -sS http://127.0.0.1:8000/harnesses
```

Inspect the worker payload that would be handed to an ECS/Fargate task:

```bash
curl -sS http://127.0.0.1:8000/jobs/<job_id>/worker-payload
```

Inspect the dry-run ECS request shape. This does not call AWS:

```bash
curl -sS http://127.0.0.1:8000/integrations/cloud/status
curl -sS http://127.0.0.1:8000/jobs/<job_id>/cloud-dispatch-plan
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

Optional local API-key enforcement and per-user token-budget quota:

```bash
export AGENT_CLOUD_API_KEYS="dev-key"
export AGENT_CLOUD_USER_TOKEN_QUOTA=20000
curl -sS http://127.0.0.1:8000/auth/status
curl -sS http://127.0.0.1:8000/users/local-user/quota
```

## Git Jobs

Use `repo_provider=git` for provider-agnostic Git remotes. The service will
clone `git_url`, create or reuse `agent/<job_id>`, commit the agent changes, and
push `refs/heads/agent/<job_id>` back to `origin`. It returns a review ref like
`git://review/agent/<job_id>` because generic Git does not have a standard PR
API.

```bash
curl -X POST http://127.0.0.1:8000/run-code-job \
  -H 'content-type: application/json' \
  -d '{
    "prompt": "For my shopping website, create a buy button.",
    "repo_provider": "git",
    "git_url": "https://git.example.com/owner/repo.git",
    "base_branch": "main",
    "deploy_policy": "pr_only"
  }'
```

Do not embed credentials in `git_url`; validation rejects URL userinfo. Use the
runtime Git credential helper, SSH agent, or `GIT_HTTP_EXTRAHEADER` in the
worker environment for private remotes.

## GitHub App Jobs

Set these environment variables in the API/worker runtime to enable real GitHub
App PR sync:

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_INSTALLATION_ID=987654
export GITHUB_APP_PRIVATE_KEY="$(cat /path/to/private-key.pem)"
```

Optional:

```bash
export GITHUB_API_URL=https://api.github.com
```

Submit a GitHub-backed job when you want GitHub App auth and PR creation:

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
- `lab_runs`: terminal run index for model/agent promotion summaries.
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
run, continuation, model/agent payload fields, lab-run list and summary,
promotion decision, and budget-stop failure.

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

- local repo copy by default
- generic Git clone/sync for provider-agnostic remotes
- GitHub App clone/PR sync only when credentials exist
- SQLite queued-job claim instead of SQS
- local Docker/worker contract instead of ECS/Fargate
- local SQLite instead of managed Postgres/DynamoDB
- local mock PR artifact for local jobs; pushed review ref for generic Git jobs;
  real GitHub PR path for GitHub jobs
- local mock deployment artifact instead of AWS deploy

That keeps the full flow testable before replacing each local component with a
cloud-backed implementation.
