from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_agent_service.models import (  # noqa: E402
    JobRequest,
    JobStatus,
    TaskCase,
    TaskSuite,
)
from cloud_agent_service.pipeline import AgentCloudFlow  # noqa: E402
from cloud_agent_service.store import JobStore  # noqa: E402
from cloud_agent_service.task_corpus import default_replayable_corpus  # noqa: E402


def default_suite() -> TaskSuite:
    return default_replayable_corpus()


def build_shopping_repo(root: Path, task_id: str) -> Path:
    repo = root / task_id / "shopping_site"
    repo.mkdir(parents=True)
    (repo / "index.html").write_text(
        "<!doctype html>\n<html>\n<body>\n<h1>Shop</h1>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return repo


def evaluate_suite(suite: TaskSuite | None = None) -> dict[str, Any]:
    suite = suite or default_suite()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        flow = AgentCloudFlow(
            store=JobStore(root / "jobs.sqlite3"),
            workspace_root=root / "workspaces",
            artifacts_dir=root / "artifacts",
        )
        task_results = [_run_case(flow, root, case) for case in suite.cases]
        checks_total = sum(len(result["checks"]) for result in task_results)
        checks_passed = sum(
            1 for result in task_results for passed in result["checks"].values() if passed
        )
        return {
            "suite_id": suite.suite_id,
            "score": checks_passed / checks_total if checks_total else 0.0,
            "checks_passed": checks_passed,
            "checks_total": checks_total,
            "tasks": task_results,
            "lab_summary": flow.store.lab_summary(),
            "leaderboard": flow.store.lab_leaderboard(),
        }


def _run_case(flow: AgentCloudFlow, root: Path, case: TaskCase) -> dict[str, Any]:
    repo = build_shopping_repo(root, case.task_id)
    job_id = flow.create_job(
        JobRequest(
            prompt=case.prompt,
            repo_path=str(repo),
            deploy_policy=case.deploy_policy,
            harness_id=case.harness_id,
            token_budget=case.token_budget,
            max_changed_files=case.max_changed_files,
        )
    )
    result = flow.run_job(job_id)
    lab_run = flow.store.get_lab_run(job_id) or {}
    workspace_index = root / "workspaces" / job_id / "repo" / "index.html"
    workspace_html = workspace_index.read_text(encoding="utf-8") if workspace_index.exists() else ""
    checks = {
        "job_status": result.status == case.expected_job_status,
        "promotion_status": result.promotion_decision.get("status")
        == case.expected_promotion_status.value,
        "lab_run_indexed": lab_run.get("promotion_status")
        == case.expected_promotion_status.value,
        "expected_changed_files": all(
            path in result.changed_files for path in case.expected_changed_files
        ),
    }
    run_artifact = result.evidence.get("run_artifact", {})
    if case.expected_job_status == JobStatus.SUCCEEDED:
        checks["run_artifact_complete"] = run_artifact.get("complete") is True
        checks["artifact_policy_gate"] = (
            result.policy_gate_results.get("artifact_policy") is True
        )
    else:
        checks["run_artifact_not_required"] = run_artifact == {}
    if "index.html" in case.expected_changed_files:
        checks["buy_button_present"] = 'data-agent="buy-button"' in workspace_html
    return {
        "task_id": case.task_id,
        "job_id": job_id,
        "score": sum(1 for passed in checks.values() if passed) / len(checks),
        "checks": checks,
        "expected": asdict(case),
        "actual": {
            "job_status": result.status.value,
            "promotion_status": result.promotion_decision.get("status"),
            "deployment_status": result.deployment_status,
            "harness_id": case.harness_id,
            "changed_files": result.changed_files,
            "tests_failed": result.tests_failed,
            "run_artifact_complete": run_artifact.get("complete"),
        },
    }


def main() -> None:
    print(json.dumps(evaluate_suite(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
