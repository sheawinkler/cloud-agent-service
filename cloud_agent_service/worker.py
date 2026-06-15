from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from cloud_agent_service.lab_warehouse import LabWarehouse
from cloud_agent_service.pipeline import AgentCloudFlow
from cloud_agent_service.store import JobStore


def build_flow() -> AgentCloudFlow:
    runtime_root = Path(os.environ.get("AGENT_CLOUD_RUNTIME", ".runtime"))
    store = JobStore.from_env(runtime_root)
    lab_warehouse = LabWarehouse.from_env(runtime_root)
    return AgentCloudFlow(
        store=store,
        workspace_root=os.environ.get("AGENT_CLOUD_WORKSPACES", str(runtime_root / "workspaces")),
        artifacts_dir=os.environ.get("AGENT_CLOUD_ARTIFACTS", str(runtime_root / "artifacts")),
        lab_warehouse=lab_warehouse,
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
    parser.add_argument(
        "--status-callback-url",
        default=os.environ.get("AGENT_CLOUD_STATUS_CALLBACK_URL", ""),
    )
    args = parser.parse_args()

    if not args.job_id and not args.claim_next:
        raise SystemExit("--job-id, AGENT_JOB_ID, or --claim-next is required")

    flow = build_flow()
    worker_id = os.environ.get("AGENT_CLOUD_WORKER_ID", "cloud_agent_service.worker")
    if args.job_id:
        _post_callback(
            args.status_callback_url,
            "started",
            "running",
            {"worker": "cloud_agent_service.worker", "worker_id": worker_id},
        )
    try:
        result = flow.run_next_queued_job() if args.claim_next else flow.run_job(args.job_id)
    except Exception as exc:
        if args.job_id:
            _post_callback(
                args.status_callback_url,
                "failed",
                "failed",
                {"error": str(exc), "worker_id": worker_id},
            )
        raise
    payload = {"status": "idle"} if result is None else asdict(result)
    if result is not None:
        _post_callback(
            args.status_callback_url,
            "completed",
            result.status.value,
            {"job_id": result.job_id, "tests_failed": result.tests_failed, "worker_id": worker_id},
        )
    if args.result_path:
        Path(args.result_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.result_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _post_callback(
    status_callback_url: str,
    callback_type: str,
    status: str,
    payload: dict[str, object],
) -> None:
    if not status_callback_url.startswith(("http://", "https://")):
        return
    url = status_callback_url.rstrip("/") + "/worker-callback"
    body = json.dumps(
        {
            "callback_type": callback_type,
            "status": status,
            "payload": payload,
        }
    ).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5):
            return
    except URLError:
        return


if __name__ == "__main__":
    main()
