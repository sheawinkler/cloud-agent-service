from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from cloud_agent_service.pipeline import AgentCloudFlow
from cloud_agent_service.store import JobStore


def build_flow() -> AgentCloudFlow:
    runtime_root = Path(os.environ.get("AGENT_CLOUD_RUNTIME", ".runtime"))
    store = JobStore(os.environ.get("AGENT_CLOUD_DB", str(runtime_root / "jobs.sqlite3")))
    return AgentCloudFlow(
        store=store,
        workspace_root=os.environ.get("AGENT_CLOUD_WORKSPACES", str(runtime_root / "workspaces")),
        artifacts_dir=os.environ.get("AGENT_CLOUD_ARTIFACTS", str(runtime_root / "artifacts")),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one local agent job.")
    parser.add_argument("--job-id", default=os.environ.get("AGENT_JOB_ID"))
    parser.add_argument("--result-path", default=os.environ.get("AGENT_RESULT_PATH"))
    args = parser.parse_args()

    if not args.job_id:
        raise SystemExit("--job-id or AGENT_JOB_ID is required")

    result = build_flow().run_job(args.job_id)
    payload = asdict(result)
    if args.result_path:
        Path(args.result_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.result_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
