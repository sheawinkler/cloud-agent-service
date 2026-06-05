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
| Safety | Failed tests or policies stop sync/deploy. | Gate failure returns `failed` before mock PR/deploy. |
| Approval gates | Deployment does not happen without policy approval. | Manual jobs return `ready` until approved. |
| Operator UX | A reviewer can see what happened quickly. | Final result includes changed files, commands, gates, and risks. |
| Cloud readiness | Local parts map cleanly to managed services. | Queue, worker, store, sync, and deploy are separate seams. |

## Core Metrics

- Request rejection rate by reason.
- Time from request accepted to worker dispatched.
- Time from worker dispatched to final result.
- Test pass rate by repo type.
- Policy gate failure rate by gate.
- Jobs stopped before deploy due to validation.
- Average changed files per job.
- Average token budget requested per job.
- Token budget consumed per stage.
- Cost per successful job.
- Jobs requiring human approval.
- Jobs retried after failure or cancellation.

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
   - Expected: score is `1.0` for the buy-button task.

## Evidence To Show In A Demo

The demo should make these proof points visible without much narration:

```text
job_created
job_queued
agent_dispatched
budget_charged
repo_cloned
repo_analyzed
prompt_upgraded
plan_created
dependencies_requested
files_changed
tests_finished
policy_gate_result
pr_created_or_updated
deployment_finished
job_succeeded
```

The service should be considered not production-ready until real GitHub sync,
real deployment integration, auth, multi-tenant quotas, and durable cloud
storage replace the local mocks.
