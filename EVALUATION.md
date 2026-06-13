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
| Auditability | The job can be reconstructed from events. | SQLite events show each major transition. |
| Budget control | Work stops before spend exceeds policy. | Budget ledger records each stage and tiny budgets fail before sync. |
| Repo intelligence | The service knows what kind of repo it is editing. | `repo_analyzed` records framework, package manager, and test hints. |
| Repo memory | Follow-up jobs can reuse prior repo context. | `repo_memory_loaded` is emitted and `repo_memory` records the last profile. |
| Model/agent/harness tracking | A repo update is attributable to a specific lab config. | Worker payload and result evidence include `ModelSpec`, `AgentSpec`, and `HarnessSpec`. |
| Lab history | Model/agent/harness runs can be compared across jobs. | `lab_runs` records terminal runs and `/lab/summary` groups promotions. |
| Leaderboard | Strong model/agent/harness tuples are ranked from terminal outcomes. | `/lab/leaderboard` reports promotion rate and average run metrics. |
| Harness portability | The control plane can route to known and custom agent harness contracts. | `/harnesses` exposes a ranked registry with a top-20 slice, and `custom:<name>` harness IDs are accepted as dispatch contracts. |
| Harness adapter ABI | Harness execution is a stable contract rather than a hard-coded local action. | Worker payloads include `harness_adapter_contract`, and evidence includes `harness_adapter_result`. |
| Security profiles | Harnesses declare command, secret, path, network, and runtime boundaries. | Worker payloads and evidence include `security_profile`. |
| Replay artifacts | Successful runs leave replayable transcript and diff evidence. | `artifacts/runs/<job_id>/run-artifact.json` is complete before promotion. |
| Analysis lab | Model/agent/harness experiments can be grouped and reported. | `/analysis/cases` and experiment reports classify runs and outcomes. |
| SLM dataset export | Replay evidence can become redacted train/eval/holdout data. | `/datasets/exports` and `scripts/export_slm_dataset.py` write JSONL splits. |
| Lab router | New work can be routed from evidence instead of hard-coded defaults. | `/lab/router/recommend` returns a `RoutingDecision`; default `fixed` behavior remains unchanged. |
| Safety | Failed tests or policies stop sync/deploy. | Gate failure returns `failed` before mock PR/deploy. |
| Preview proof | Reviewers get inspectable evidence before deploy. | Final result includes preview URL, preview artifact, and browser-proof checks. |
| Promotion decision | The run has a clear model/agent verdict. | Final result returns `promote`, `reject`, or `needs_review` with evidence. |
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
- Generic Git sync success rate.
- GitHub App sync success rate.
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
   - Expected: job state and event history remain readable from SQLite.

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
policy_gate_result
branch_pushed
pr_created_or_updated
deployment_finished
job_succeeded
promotion_decision_created
```

The service should be considered not production-ready until real deployment
integration, durable cloud storage, and actual ECS/Fargate worker submission
replace the local defaults. The model/agent/harness lab layer records run
metadata and promotion decisions, and `lab_runs` makes those decisions queryable
for comparison; the OpenAI Responses path is present but disabled unless
configured. Ranked and custom harness IDs are dispatch contracts, not proof that
third-party CLIs or managed agents execute, until corresponding worker adapters
are built and verified. Generic Git sync is provider agnostic, while GitHub PR
creation is implemented only for `repo_provider=github` when app credentials are
configured and verified.
