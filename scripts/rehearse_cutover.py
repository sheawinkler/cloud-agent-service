from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud_agent_service.cloud_dispatch import EcsDispatchPlanner  # noqa: E402
from cloud_agent_service.cutover import (  # noqa: E402
    DEFAULT_CUTOVER_PROMPT,
    DEFAULT_STATUS_CALLBACK_URL,
    CutoverRehearsal,
)
from cloud_agent_service.lab_warehouse import LabWarehouse  # noqa: E402
from cloud_agent_service.pipeline import AgentCloudFlow  # noqa: E402
from cloud_agent_service.store import JobStore  # noqa: E402


def build_flow(runtime_root: Path) -> AgentCloudFlow:
    return AgentCloudFlow(
        store=JobStore(runtime_root / "jobs.sqlite3", provider="sqlite"),
        workspace_root=runtime_root / "workspaces",
        artifacts_dir=runtime_root / "artifacts",
        lab_warehouse=LabWarehouse(runtime_root / "lab.duckdb"),
    )


def seed_repo(root: Path) -> Path:
    repo = root / "cutover_rehearsal_repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "index.html").write_text(
        "<!doctype html>\n<html>\n<body>\n<h1>Shop</h1>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return repo


def rehearse_cutover(
    *,
    repo_path: str | None = None,
    runtime_root: str | None = None,
    prompt: str = DEFAULT_CUTOVER_PROMPT,
    status_callback_url: str = DEFAULT_STATUS_CALLBACK_URL,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        selected_runtime = Path(runtime_root) if runtime_root else temp_root / "runtime"
        repo_seed_root = selected_runtime if runtime_root else temp_root
        selected_repo = Path(repo_path) if repo_path else seed_repo(repo_seed_root)
        flow = build_flow(selected_runtime)
        return CutoverRehearsal(flow, EcsDispatchPlanner()).rehearse(
            repo_path=str(selected_repo),
            prompt=prompt,
            status_callback_url=status_callback_url,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a deterministic production cutover rehearsal without live cloud calls."
    )
    parser.add_argument("--repo-path", help="target repo path; omitted creates a temp repo")
    parser.add_argument(
        "--runtime-root",
        help="runtime root for job store/artifacts; omitted uses a temp runtime",
    )
    parser.add_argument("--prompt", default=DEFAULT_CUTOVER_PROMPT)
    parser.add_argument("--status-callback-url", default=DEFAULT_STATUS_CALLBACK_URL)
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="exit nonzero if readiness critical blockers remain",
    )
    args = parser.parse_args()

    report = rehearse_cutover(
        repo_path=args.repo_path,
        runtime_root=args.runtime_root,
        prompt=args.prompt,
        status_callback_url=args.status_callback_url,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["ok"]:
        return 1
    readiness = report["readiness"]
    if args.require_production_ready and not readiness["production_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
