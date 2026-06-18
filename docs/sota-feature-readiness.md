# SOTA Feature Readiness

This repo is no longer just a local "make a buy button" demo. The strongest
remaining product shape is a control plane that can prove which parts are live,
which parts are env-gated, and which parts are still provider contracts.

Runtime source of truth:

```bash
python3 scripts/doctor.py --json
python3 scripts/rehearse_cutover.py
curl -sS http://127.0.0.1:8000/readiness/scorecard
curl -sS http://127.0.0.1:8000/readiness/features
curl -sS http://127.0.0.1:8000/cutover/status
```

## Feature Families

| Family | Capability | Runtime Status Source |
| --- | --- | --- |
| Control plane | bounded request intake, prompt upgrade, worker payloads | `/jobs`, `/jobs/{id}/worker-payload` |
| Cloud execution | ECS submit, worker leases, signed callbacks | `/integrations/cloud/e2e-status`, `/jobs/leases` |
| Evidence | run artifacts, artifact refs, provenance, promotion gates | `/jobs/{id}/artifacts`, `/jobs/{id}/provenance` |
| Language Model Lab | analysis cases, experiments, SLM datasets, router | `/analysis/cases`, `/datasets/exports`, `/lab/router/recommend` |
| Analytics | DuckDB lab warehouse, leaderboard, summaries | `/lab/warehouse/status`, `/lab/leaderboard` |
| Storage | SQLite local default, Postgres optional operational adapter | `/integrations/database/status` |
| Git forge | generic Git, GitHub App, non-GitHub review contracts | `/integrations/forge/status` |
| Model runtime | local deterministic, OpenAI Responses gated path | `/models` |
| Harness runtime | top-20 registry, local/Pi/OpenAI adapters, custom contracts | `/harnesses` |
| Automation | signed idempotent webhook/event intake | `/integrations/events/status`, `/events/intakes` |
| Operations | readiness scorecard, doctor, and cutover rehearsal | `/readiness/scorecard`, `/cutover/status`, `scripts/rehearse_cutover.py` |
| Multi-tenant | API keys, quota guard, user-scoped jobs | `/auth/status`, `/users/{id}/quota` |

## Still Not Claimed Live By Default

- AWS worker execution: live only after ECS env plus
  `AGENT_CLOUD_ECS_SUBMIT_ENABLED=1`.
- Public worker callbacks: authenticated only after
  `AGENT_CLOUD_WORKER_CALLBACK_SECRET`.
- Public webhook intake: signed only after `AGENT_CLOUD_EVENT_INGEST_SECRET`.
- GitHub App PRs: live only after GitHub App credentials are configured.
- GitLab, Bitbucket, and Gitea native review creation: modeled as contracts
  until provider-specific adapters are implemented.
- OpenAI model/edit path: live only after explicit enable flags and
  `OPENAI_API_KEY`.
- Postgres operational store: live only after
  `AGENT_CLOUD_DB_PROVIDER=postgres`, a DSN, and `psycopg`.
- Vercel preview/Sandbox: contract unless explicit provider flags and tokens
  are configured and verified.
- Cutover rehearsal: local proof only; it does not submit ECS, push Git
  branches, deploy, or clear production readiness blockers.

## Event Intake Contract

`POST /events/intake` accepts a JSON object with optional direct job fields:

```json
{
  "source": "github",
  "event_type": "issues",
  "idempotency_key": "github:issue:123",
  "prompt": "For my shopping website, create a buy button.",
  "repo_path": "/host_repo",
  "deploy_policy": "manual",
  "run_immediately": false
}
```

If `AGENT_CLOUD_EVENT_INGEST_SECRET` is set, callers must send
`x-agent-cloud-event-signature` as `sha256=<hmac_hex>` over the raw request
body. Event rows are deduped by `idempotency_key`; provider retries return the
existing intake/job instead of creating duplicate work.

If the event lacks a repo target, the service records `accepted_no_job` and does
not dispatch a worker.

## Operator Cutover Gates

Before calling this production-ready, require the scorecard to have no critical
blockers:

```bash
python3 scripts/doctor.py --require-production-ready
```

That command is intentionally stricter than the local demo path. Local demos can
be excellent while production readiness remains env-gated.

Run a local production cutover rehearsal before an operator cutover:

```bash
python3 scripts/rehearse_cutover.py
curl -sS http://127.0.0.1:8000/cutover/status
curl -sS -X POST http://127.0.0.1:8000/cutover/rehearse \
  -H 'content-type: application/json' \
  -d '{"repo_path":"/host_repo","status_callback_url":"https://api.example.com/jobs"}'
```

Expected: `cutover-rehearsal.v1` reports `ok: true`,
`live_external_calls_made: false`, redacted callback tokens in the ECS dry-run
request, and a `cutover_decision` that remains `blocked_for_production` while
critical blockers remain.
