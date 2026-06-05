from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_agent_service.models import DeploymentPolicy, JobRequest, JobStatus
from cloud_agent_service.pipeline import AgentCloudFlow
from cloud_agent_service.store import JobStore


def build_shopping_repo(root: Path) -> Path:
    repo = root / "shopping_site"
    repo.mkdir()
    (repo / "index.html").write_text(
        "<!doctype html>\n<html>\n<body>\n<h1>Shop</h1>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return repo


def evaluate_buy_button() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = build_shopping_repo(root)
        flow = AgentCloudFlow(
            store=JobStore(root / "jobs.sqlite3"),
            workspace_root=root / "workspaces",
            artifacts_dir=root / "artifacts",
        )
        job_id = flow.create_job(
            JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_path=str(repo),
                deploy_policy=DeploymentPolicy.LOCAL,
            )
        )
        result = flow.run_job(job_id)
        workspace_index = root / "workspaces" / job_id / "repo" / "index.html"
        rendered_change = workspace_index.read_text(encoding="utf-8")
        checks = {
            "job_succeeded": result.status == JobStatus.SUCCEEDED,
            "buy_button_present": 'data-agent="buy-button"' in rendered_change,
            "tests_passed": not result.tests_failed,
            "policy_passed": all(result.policy_gate_results.values()),
            "pr_artifact_created": (root / "artifacts" / f"{job_id}-pr.json").exists(),
            "deployment_artifact_created": (
                root / "artifacts" / f"{job_id}-deployment.json"
            ).exists(),
        }
        score = sum(1 for value in checks.values() if value) / len(checks)
        return {
            "task": "shopping_buy_button",
            "score": score,
            "checks": checks,
            "result": asdict(result),
        }


def main() -> None:
    print(json.dumps(evaluate_buy_button(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
