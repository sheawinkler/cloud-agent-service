from __future__ import annotations

import json
import sys
import tempfile
from argparse import ArgumentParser
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
    parser = ArgumentParser(description="Run the local happy-path demo.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full JSON result instead of the concise demo summary.",
    )
    args = parser.parse_args()

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
        payload = {
            "job_id": job_id,
            "status": result.status,
            "changed_files": result.changed_files,
            "tests_failed": result.tests_failed,
            "deployment_status": result.deployment_status,
            "preview_url": result.evidence.get("preview_url"),
            "browser_checks": result.evidence.get("browser_checks", {}),
            "events": [event["event_type"] for event in result.events],
            "result": asdict(result),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return

        print("Cloud Agent Service demo")
        print(f"status: {result.status}")
        print(f"job_id: {job_id}")
        print(f"changed_files: {', '.join(result.changed_files)}")
        print(f"tests_failed: {len(result.tests_failed)}")
        print(f"preview: {result.evidence.get('preview_url')}")
        print(f"deployment: {result.deployment_status}")
        print("events:")
        for event in payload["events"]:
            print(f"  - {event}")


if __name__ == "__main__":
    main()
