# Evaluation

Cloud Agent Service should be evaluated as the control loop around a coding
agent, not as a model demo. The important question is whether the service can
turn a user request into a bounded, inspectable, tested change without crossing
the deploy boundary when evidence is weak.

## Success Criteria

| Area | What Good Looks Like | MVP Signal |
| --- | --- | --- |
| Request handling | Invalid work is rejected before dispatch. | Oversized or empty prompts fail fast. |
| Prompt quality | The raw request becomes a concise implementation brief. | `prompt_upgraded` event is recorded. |
| Isolation | Each job gets its own workspace. | Workspace lives under `workspaces/<job_id>/repo`. |
| Determinism | Checks and gates are repeatable. | `python3 -m compileall .` is run for every job. |
| Auditability | The job can be reconstructed from events. | Configured store events show each major transition. |
| Budget control | Work stops before spend exceeds policy. | Budget ledger records each stage and tiny budgets fail before sync. |
| Repo intelligence | The service knows what kind of repo it is editing. | `repo_analyzed` records framework, package manager, and test hints. |
| Repo memory | Follow-up jobs can reuse prior repo context. | `repo_memory_loaded` is emitted and `repo_memory` records the last profile. |
| Model/agent/harness tracking | A repo update is attributable to a specific lab config. | Worker payload and result evidence include `ModelSpec`, `AgentSpec`, and `HarnessSpec`. |
| Lab history | Model/agent/harness runs can be compared across jobs. | `lab_runs` records terminal runs and `/lab/summary` groups promotions. |
| Lab warehouse | Lab analytics are separated from operational writes. | `/lab/warehouse/status` reports the DuckDB read model and materialized run count. |
| Leaderboard | Strong model/agent/harness tuples are ranked from terminal outcomes. | `/lab/leaderboard` reports promotion rate and average run metrics. |
| Harness portability | The control plane can route to known and custom agent harness contracts. | `/harnesses` exposes a ranked registry with a top-20 slice, and `custom:<name>` harness IDs are accepted as dispatch contracts. |
| Harness adapter ABI | Harness execution is a stable contract rather than a hard-coded local action. | Worker payloads include `harness_adapter_contract`, and evidence includes `harness_adapter_result`. |
| Security profiles | Harnesses declare command, secret, path, network, and runtime boundaries. | Worker payloads and evidence include `security_profile`. |
| Replay artifacts | Successful runs leave replayable transcript and diff evidence. | `artifacts/runs/<job_id>/run-artifact.json` is complete before promotion. |
| Analysis lab | Model/agent/harness experiments can be grouped and reported. | `/analysis/cases` and experiment reports classify runs and outcomes. |
| SLM dataset export | Replay evidence can become redacted train/eval/holdout data. | `/datasets/exports` and `scripts/export_slm_dataset.py` write JSONL splits. |
| Lab router | New work can be routed from evidence instead of hard-coded defaults. | `/lab/router/recommend` returns a `RoutingDecision`; default `fixed` behavior remains unchanged. |
| Cloud worker execution | ECS submission is possible only behind explicit env gates. | `/cloud-dispatch-plan` stays dry-run; `/cloud-dispatch` persists submitted or failed dispatch records. |
| Worker callbacks | Cloud workers can report progress without trusting logs alone. | `/worker-callback` records started, heartbeat, artifact, completed, and failed callbacks. |
| Callback auth | Worker callbacks can be signed without storing raw tokens in dispatch records. | `AGENT_CLOUD_WORKER_CALLBACK_SECRET` enables `x-agent-cloud-callback-token`; plans and records redact the token. |
| Worker leases | Active workers have recoverable control-plane state. | `/jobs/<id>/leases` shows owner, provider, heartbeat, expiry, and status. |
| Artifact storage | Run artifacts have durable refs independent of local file paths. | `/jobs/<id>/artifacts` returns provider, URI, digest, and size. |
| Experiment batches | Analysis experiments can fan out as bounded batches. | `/analysis/experiments/<id>/batch` records batch status and linked job IDs. |
| Dataset lineage | SLM exports include split policy, redaction policy, fingerprints, and holdout guardrails. | Dataset manifest lineage marks holdout as evaluation-only. |
| Database provider | Operational writes, lab analytics, and production target are distinct. | `/integrations/database/status` reports SQLite operational state, DuckDB lab warehouse, and optional Postgres adapter readiness. |
| Deployment provider | Deploy behavior is a provider contract instead of a hard-coded string. | `/integrations/deploy/status` reports local mock or Vercel preview mode. |
| Execution provider | Worker execution target is explicit. | `/integrations/execution/status` reports local, ECS/Fargate, or Vercel Sandbox contract mode. |
| Forge provider | Git review output is not locked to GitHub. | `/integrations/forge/status` reports generic Git plus GitHub/GitLab/Bitbucket/Gitea review capabilities. |
| OpenAI edit adapter | A live model-backed edit path exists behind explicit gates. | `openai-codex-cli` can use `AGENT_CLOUD_ENABLE_OPENAI_EDIT_ADAPTER` plus `OPENAI_API_KEY`; otherwise it falls back to local contract execution. |
| Lab appliance | The lab can be demonstrated end to end without external services. | `scripts/demo_lab_in_a_box.py` seeds a repo, runs a lab job, exports JSONL, refreshes the warehouse, and reports router/leaderboard state. |
| Readiness scorecard | Production gaps are executable status, not hand-wavy roadmap prose. | `/readiness/scorecard`, `/readiness/features`, and `scripts/doctor.py` report ready/local-ready/env-gated/partial/contract capabilities and critical blockers. |
| Cutover rehearsal | Operators can prove the cloud cutover contract without touching live cloud resources. | `/cutover/status`, `/cutover/rehearse`, and `scripts/rehearse_cutover.py` prove signed callbacks, signed event intake, redacted ECS dry-run shape, and blocker binding. |
| Event intake | External events can create bounded jobs safely. | `/events/intake` verifies HMAC when configured, dedupes by idempotency key, redacts payloads, and only dispatches when a repo target exists. |
| Provenance | Successful runs leave a compact manifest tying evidence to hashes. | `/jobs/<id>/provenance` returns manifest path, digest, deployment record, and source fingerprints. |
| Safety | Failed tests or policies stop sync/deploy. | Gate failure returns `failed` before mock PR/deploy. |
| Preview proof | Reviewers get inspectable evidence before deploy. | Final result includes preview URL, preview artifact, and browser-proof checks. |
| Promotion decision | The run has a clear model/agent verdict. | Final result returns `promote`, `reject`, or `needs_review` with evidence. |
| Promotion comparison | Candidate tuples are judged against a baseline. | Promotion evidence includes `promotion-evaluation.v1` with candidate and baseline metrics. |
| Task-suite comparison | Multiple scenarios can be compared as a lab batch. | `scripts/evaluate_task_suite.py` scores the shared `/tasks/corpus` cases. |
| Git sync | Jobs are not locked to one Git forge. | Generic Git jobs clone a remote and push a review branch. |
| GitHub sync | GitHub jobs can use app-scoped credentials instead of user tokens. | Configured GitHub App jobs clone, push a branch, and create or reuse a PR. |
| Auth and quotas | Multi-tenant controls can be enabled without changing handlers. | `AGENT_CLOUD_API_KEYS` and `AGENT_CLOUD_USER_TOKEN_QUOTA` gate requests. |
| Approval gates | Deployment does not happen without policy approval. | Manual jobs return `ready` until approved. |
| Operator UX | A reviewer can see what happened quickly. | Final result includes changed files, commands, gates, and risks. |
| Cloud readiness | Local parts map cleanly to managed services. | ECS dispatch plans map jobs to Fargate `run_task` requests without AWS calls. |

## Core Metrics

- Request rejection rate by reason.
- Time from request accepted to worker dispatched.
- Time from worker dispatched to final result.
- Test pass rate by repo type.
- Policy gate failure rate by gate.
- Jobs stopped before deploy due to validation.
- Preview proof pass rate.
- Promotion status distribution by model, agent, and harness.
- Harness dispatch plans by promotion status.
- Harness adapter execution status by harness.
- Security profile selected by harness.
- Run artifact completion rate.
- Promotion rate by model/agent/harness tuple.
- Analysis experiment score by case.
- Dataset export counts by split.
- Router confidence and fallback rate.
- Cloud dispatch submit success/failure rate.
- Worker callback heartbeat age.
- Callback auth configured rate and rejected callback count.
- Artifact reference completeness by job.
- Experiment batch completion/failure counts.
- Database provider in use by run environment.
- Deployment provider success/failure or dry-run contract count.
- Execution provider selected by job.
- Provenance manifest completion and hash availability.
- Generic Git sync success rate.
- GitHub App sync success rate.
- Forge-native review coverage by provider.
- Lab appliance demo success rate.
- Readiness score and critical blocker count.
- Cutover rehearsal pass/fail count and remaining production blockers.
- Event intake accepted/rejected/duplicate counts.
- Event intake signature rejection count.
- Average changed files per job.
- Average token budget requested per job.
- Token budget consumed per stage.
- Cost per successful job.
- Jobs requiring human approval.
- Jobs retried after failure or cancellation.
- Task-suite score by model/agent/harness tuple.
- Replay corpus score by case.
- User token budget reserved versus consumed.
- ECS dispatch plans created versus rejected for missing configuration.

## Evaluation Scenarios

1. Happy path
   - Submit: "For my shopping website, create a buy button."
   - Expected: job succeeds, `index.html` changes, tests pass, mock PR/deploy
     artifacts are recorded.

2. Invalid input
   - Submit an empty prompt or oversized prompt.
   - Expected: request fails before job dispatch.

3. Gate failure
   - Force a test failure or policy violation.
   - Expected: job fails before mock PR sync and before mock deployment.

4. Workspace isolation
   - Run two jobs against the same source repo.
   - Expected: separate workspace directories and separate event histories.

5. Restart recovery
   - Restart the API after a completed job.
   - Expected: job state and event history remain readable from the configured store.

6. Budget stop
   - Submit a valid request with a token budget below the first-stage estimate.
   - Expected: job fails with `not deployed: budget exceeded`, with no PR/deploy.

7. Manual approval
   - Submit a valid request with `deploy_policy=manual`.
   - Expected: job succeeds with `ready: manual approval required`; deployment
     artifact appears only after `/approve-deployment`.

8. Golden task score
   - Run `python3 scripts/evaluate_mvp.py`.
   - Expected: score is `1.0` for the buy-button task, including promotion
     decision creation.

9. GitHub App readiness
   - Configure `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and
     `GITHUB_APP_PRIVATE_KEY`, then submit `repo_provider=github`.
   - Expected: status reports configured, the repo is cloned by installation
     token, an agent branch is pushed, and a PR URL is returned.

10. Generic Git remote
    - Submit `repo_provider=git` with a private or public `git_url` available
      to the worker runtime.
    - Expected: the repo is cloned, an agent branch is pushed to `origin`, and
      the result returns a `git://review/...` review ref.

11. Conversation continuation
    - Run `/jobs/<job_id>/continue` after a completed or failed job.
    - Expected: the child job records `parent_job_id`, reuses the same provider
      and branch lineage, and emits its own evidence.

12. Deployment policy matrix
    - Submit equivalent jobs with `manual`, `local`, `never`, `pr_only`,
      `preview_only`, `staging_auto`, and `production_approval`.
    - Expected: each policy returns the documented deploy status and only writes
      deployment artifacts for deploy-capable local policies.

13. Model/agent/harness lab run
    - Submit a valid job with `model_id=local-deterministic` and
      `agent_id=repo-editor-v1` and `harness_id=local-template`.
    - Expected: worker payload and final evidence include model, agent, and
      harness specs, and the result includes a promotion decision.

14. Lab run summary
    - Run successful, review-required, and failed jobs.
    - Expected: `/lab/runs` lists terminal runs and `/lab/summary` reports
      promotion counts by status, model/agent pair, and harness.

15. Task-suite comparison
    - Run `python3 scripts/evaluate_task_suite.py`.
    - Expected: the replay corpus scores `1.0` across promote, needs-review,
      and reject outcomes, writes all terminal runs into `lab_runs`, and returns
      leaderboard rows.

16. Auth and quota controls
    - Set `AGENT_CLOUD_API_KEYS` and `AGENT_CLOUD_USER_TOKEN_QUOTA`, then submit
      jobs with and without `x-api-key`.
    - Expected: unauthenticated requests are rejected and quota-exceeding
      requests return `429` before dispatch.

17. ECS dispatch contract
    - Set `AGENT_CLOUD_ECS_CLUSTER`, `AGENT_CLOUD_ECS_TASK_DEFINITION`, and
      `AGENT_CLOUD_ECS_SUBNETS`, then call `/jobs/<job_id>/cloud-dispatch-plan`.
    - Expected: the response includes an AWS ECS `run_task` request shape and
      the worker payload; no AWS API call is made.

18. Harness index and custom harness contract
    - Read `/harnesses`, then submit equivalent jobs with `local-template`,
      an indexed harness ID such as `factory-droid`, `pi-coding-agent`, or
      `hermes-agent`, and a custom ID such as `custom:internal-runner`.
    - Expected: `/harnesses` returns a 20-item `top_20` slice plus the full
      ranked registry, the worker payload and evidence preserve the selected
      harness contract, unknown IDs fail before dispatch, and custom IDs are
      recorded without claiming live execution.

19. Harness adapter ABI and security profile
    - Submit a default `local-template` job and inspect
      `/jobs/<job_id>/worker-payload`.
    - Expected: payload includes `harness_adapter_contract` and
      `security_profile`; final evidence includes `harness_adapter_result` and
      the selected security profile.

20. Real adapter seam
    - Enable a known external adapter such as `pi-coding-agent` with
      `AGENT_CLOUD_ENABLE_PI_CODING_AGENT=1` and an executable
      `AGENT_CLOUD_PI_CODING_AGENT_CMD`.
    - Expected: evidence reports `pi-coding-agent-adapter` with status
      `executed`; if the command is not enabled or executable, the service does
      not claim live adapter execution.

21. Replay artifact and promotion gate v2
    - Run a successful local job and inspect `evidence.run_artifact`.
    - Expected: `run-artifact.json`, transcript, and diff/fingerprint files
      exist; `artifact_policy`, `transcript_policy`, and
      `security_profile_policy` are true before a run can promote.

22. Lab leaderboard
    - Run several corpus cases and call `/lab/leaderboard`.
    - Expected: rows are grouped by model, agent, and harness with promotion
      counts, promotion rate, and average changed files/tests/tokens.

23. Analysis lab experiment
    - Create an experiment for `model_bakeoff_repo_edit`, run it against a local
      repo, then read the report.
    - Expected: the report lists linked job IDs, promotion status counts,
      failure categories, token usage, and run-artifact completeness.

24. SLM dataset export
    - Run successful jobs and call `/datasets/exports` or
      `scripts/export_slm_dataset.py`.
    - Expected: `train`, `eval`, and `holdout` JSONL files are valid, stable,
      redacted, and contain no raw private paths or secret-looking values.

25. Lab router
    - Call `/lab/router/recommend` before and after successful lab history
      exists, then submit a job with `routing_policy=auto_select`.
    - Expected: cold routing falls back to caller defaults, warm routing uses
      leaderboard evidence, and default `fixed` jobs remain unchanged.

26. Cloud worker submit contract
    - Configure ECS env and call `/cloud-dispatch-plan`.
    - Expected: dry-run request contains worker command, callback URL, and lab
      tuple env.
    - With `AGENT_CLOUD_ECS_SUBMIT_ENABLED=1`, `/cloud-dispatch` either records
      a submitted task ARN or a failed dispatch record.

27. Worker callback protocol
    - Post `started` and `heartbeat` callbacks to `/jobs/<id>/worker-callback`.
    - Expected: callbacks are listed in order and job events include
      `worker_callback_received`.

28. Artifact references
    - Run a successful job and call `/jobs/<id>/artifacts`.
    - Expected: run artifact, transcript, and diff refs include provider, URI,
      SHA-256, and byte length.

29. Experiment batch
    - Run `/analysis/experiments/<id>/batch` with `max_concurrency`.
    - Expected: batch status, requested/completed/failed counts, and job IDs are
      persisted and queryable.

30. Dataset lineage
    - Export a dataset and inspect `manifest.json`.
    - Expected: manifest includes split policy, redaction policy, source
      fingerprints, and holdout guard with `use_for_training=false`.

31. DuckDB embedded store
    - Start with `AGENT_CLOUD_DB_PROVIDER=duckdb` and run the happy path.
    - Expected: job, events, budget entries, lab run, and provenance are
      persisted; `/integrations/database/status` reports `duckdb`.

32. Vercel preview contract
    - Start with `AGENT_CLOUD_DEPLOYMENT_PROVIDER=vercel_preview` and leave
      `AGENT_CLOUD_VERCEL_DEPLOY_ENABLED` unset.
    - Expected: preview jobs record a Vercel deployment contract artifact but
      do not call Vercel.

33. Execution provider contract
    - Set `AGENT_CLOUD_EXECUTION_PROVIDER=vercel_sandbox`.
    - Expected: `/integrations/execution/status` reports sandbox contract mode;
      no live sandbox execution is claimed.

34. Provenance manifest
    - Run a successful job and call `/jobs/<id>/provenance`.
    - Expected: manifest includes changed-file fingerprints, artifact refs,
      deployment provider record, policy gates, and promotion inputs.

35. Readiness scorecard
    - Call `/readiness/scorecard`, `/readiness/features`, and
      `python3 scripts/doctor.py --json`.
    - Expected: all report `sota-readiness.v1`, include event intake and
      operator doctor capabilities, and clearly distinguish local-ready,
      env-gated, partial, and provider-contract items.

36. Cutover rehearsal
    - Run `python3 scripts/rehearse_cutover.py`, call `/cutover/status`, and
      post a local repo to `/cutover/rehearse`.
    - Expected: report schema is `cutover-rehearsal.v1`, critical local proofs
      pass, live external calls are false, callback tokens are redacted from
      the ECS dry-run request, and the decision remains blocked while critical
      readiness blockers exist.

37. Event intake
    - Post a webhook-style JSON payload with `idempotency_key`, `prompt`, and
      `repo_path` to `/events/intake`, then post it again.
    - Expected: first request creates exactly one queued job; second request
      returns `duplicate: true` with the same job id.

38. Signed event intake
    - Set `AGENT_CLOUD_EVENT_INGEST_SECRET`, send one request with a valid
      `x-agent-cloud-event-signature`, and one with an invalid signature.
    - Expected: valid request is accepted; invalid request returns `401` before
      any job is created.

## Evidence To Show In A Demo

The demo should make these proof points visible without much narration:

```text
job_created
routing_decision_created
harness_selected
job_queued
budget_charged
agent_dispatched
lab_run_configured
harness_adapter_selected
harness_security_profile_selected
repo_cloned
repo_analyzed
repo_memory_loaded
prompt_upgraded
plan_created
dependencies_requested
harness_adapter_finished
files_changed
tests_finished
preview_created
browser_proof_finished
run_artifact_created
worker_callback_received
policy_gate_result
branch_pushed
pr_created_or_updated
deployment_finished
provenance_manifest_created
job_succeeded
promotion_decision_created
```

The service should be considered not production-ready until real deployment
integration, durable cloud storage, and verified ECS/Fargate worker operation
replace the local defaults. The model/agent/harness lab layer records run
metadata and promotion decisions, and `lab_runs` makes those decisions queryable
for comparison; OpenAI Responses planning and edit paths are disabled unless
explicitly configured. Ranked and custom harness IDs are dispatch contracts, not
proof that third-party CLIs or managed agents execute, until corresponding
worker adapters are built and verified. DuckDB is an embedded local/lab backend,
not managed multi-writer production state. Postgres is available only through the
optional DSN-backed adapter. Vercel preview and Vercel Sandbox modes are provider
contracts unless their live env flags and credentials are configured and
verified. Event intake is unsigned local mode until
`AGENT_CLOUD_EVENT_INGEST_SECRET` is configured and signatures are verified.
Generic Git sync is provider agnostic, while provider-native PR/MR creation is
implemented only for GitHub App jobs today.
