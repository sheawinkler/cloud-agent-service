from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_agent_service.models import DeploymentPolicy, JobRequest
from cloud_agent_service.pipeline import AgentCloudFlow
from cloud_agent_service.store import JobStore


def build_demo_repo(root: Path) -> Path:
    repo = root / "shopping_site"
    repo.mkdir()
    (repo / "index.html").write_text(
        "<!doctype html>\n<html>\n<body>\n<h1>Shop</h1>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return repo


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_repo = build_demo_repo(root)
        flow = AgentCloudFlow(
            store=JobStore(root / "jobs.sqlite3"),
            workspace_root=root / "workspaces",
            artifacts_dir=root / "artifacts",
        )
        request = JobRequest(
            prompt="For my shopping website, create a buy button.",
            repo_path=str(source_repo),
            deploy_policy=DeploymentPolicy.LOCAL,
        )

        job_id = flow.create_job(request)
        result = flow.run_job(job_id)
        print(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": result.status,
                    "changed_files": result.changed_files,
                    "tests_failed": result.tests_failed,
                    "deployment_status": result.deployment_status,
                    "events": [event["event_type"] for event in result.events],
                    "result": asdict(result),
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
