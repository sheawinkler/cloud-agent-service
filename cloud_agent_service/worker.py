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
    parser.add_argument(
        "--claim-next",
        action="store_true",
        help="Claim and run the oldest queued job from the local store.",
    )
    parser.add_argument("--result-path", default=os.environ.get("AGENT_RESULT_PATH"))
    args = parser.parse_args()

    if not args.job_id and not args.claim_next:
        raise SystemExit("--job-id, AGENT_JOB_ID, or --claim-next is required")

    flow = build_flow()
    result = flow.run_next_queued_job() if args.claim_next else flow.run_job(args.job_id)
    payload = {"status": "idle"} if result is None else asdict(result)
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
