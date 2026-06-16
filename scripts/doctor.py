from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cloud_agent_service.cloud_dispatch import EcsDispatchPlanner  # noqa: E402
from cloud_agent_service.lab_warehouse import LabWarehouse  # noqa: E402
from cloud_agent_service.pipeline import AgentCloudFlow  # noqa: E402
from cloud_agent_service.readiness import ReadinessReporter  # noqa: E402
from cloud_agent_service.store import JobStore  # noqa: E402


def build_flow() -> AgentCloudFlow:
    runtime_root = Path(os.environ.get("AGENT_CLOUD_RUNTIME", ".runtime"))
    return AgentCloudFlow(
        store=JobStore.from_env(runtime_root),
        workspace_root=os.environ.get(
            "AGENT_CLOUD_WORKSPACES",
            str(runtime_root / "workspaces"),
        ),
        artifacts_dir=os.environ.get(
            "AGENT_CLOUD_ARTIFACTS",
            str(runtime_root / "artifacts"),
        ),
        lab_warehouse=LabWarehouse.from_env(runtime_root),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Cloud Agent Service readiness doctor")
    parser.add_argument("--json", action="store_true", help="print full JSON report")
    parser.add_argument(
        "--require-production-ready",
        action="store_true",
        help="exit nonzero if critical production blockers remain",
    )
    args = parser.parse_args()

    report = ReadinessReporter(build_flow(), EcsDispatchPlanner()).report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"schema={report['schema_version']}")
        print(f"readiness_score={report['readiness_score']}")
        print(f"production_ready={report['production_ready']}")
        print("status_counts=" + json.dumps(report["status_counts"], sort_keys=True))
        if report["critical_blockers"]:
            print("critical_blockers=" + ",".join(report["critical_blockers"]))
    if args.require_production_ready and not report["production_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
