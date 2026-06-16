from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_agent_service.cloud_dispatch import EcsDispatchPlanner
from cloud_agent_service.models import DeploymentPolicy, JobRequest, RoutingPolicy
from cloud_agent_service.pipeline import AgentCloudFlow
from cloud_agent_service.readiness import ReadinessReporter
from cloud_agent_service.store import JobStore


def build_seed_repo(root: Path) -> Path:
    repo = root / "lab_seed_repo"
    repo.mkdir()
    (repo / "index.html").write_text(
        "<!doctype html>\n<html>\n<body>\n<h1>Lab Shop</h1>\n</body>\n</html>\n",
        encoding="utf-8",
    )
    return repo


def run_lab_in_a_box() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = build_seed_repo(root)
        flow = AgentCloudFlow(
            store=JobStore(root / "jobs.sqlite3"),
            workspace_root=root / "workspaces",
            artifacts_dir=root / "artifacts",
        )
        baseline_job_id = flow.create_job(
            JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_path=str(repo),
                deploy_policy=DeploymentPolicy.LOCAL,
            )
        )
        baseline = flow.run_job(baseline_job_id)
        experiment = flow.create_analysis_experiment(
            case_id="model_bakeoff_repo_edit",
            name="lab in a box bakeoff",
        )
        experiment_run = flow.run_analysis_experiment(
            experiment.experiment_id,
            repo_path=str(repo),
            deploy_policy=DeploymentPolicy.PREVIEW_ONLY,
        )
        dataset = flow.export_slm_dataset(export_id="lab_in_a_box", limit=100)
        warehouse = flow.refresh_lab_warehouse()
        route = flow.recommend_route(
            JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_path=str(repo),
                routing_policy=RoutingPolicy.RECOMMEND_ONLY,
            )
        )
        report = flow.experiment_report(experiment.experiment_id)
        lab_summary = flow.lab_summary()
        readiness = ReadinessReporter(flow, EcsDispatchPlanner()).report()
        checks = {
            "baseline_succeeded": baseline.status.value == "succeeded",
            "baseline_promoted": baseline.promotion_decision.get("status") == "promote",
            "experiment_recorded": len(experiment_run.job_ids) >= 1,
            "dataset_exported": sum(dataset.counts.values()) >= 1,
            "warehouse_refreshed": bool(warehouse.get("ready")) or lab_summary["total_runs"] >= 1,
            "router_recommended": route.selected_harness_id == "local-template",
            "holdout_guarded": dataset.lineage["holdout_guard"]["use_for_training"] is False,
            "readiness_reported": readiness["schema_version"] == "sota-readiness.v1",
        }
        return {
            "schema_version": "lab-in-a-box-demo.v1",
            "ok": all(checks.values()),
            "checks": checks,
            "baseline_job_id": baseline_job_id,
            "experiment": asdict(experiment),
            "experiment_run": asdict(experiment_run),
            "experiment_report": asdict(report),
            "dataset_export": asdict(dataset),
            "warehouse": warehouse,
            "router_decision": asdict(route),
            "readiness": {
                "readiness_score": readiness["readiness_score"],
                "production_ready": readiness["production_ready"],
                "status_counts": readiness["status_counts"],
                "critical_blockers": readiness["critical_blockers"],
            },
            "lab_summary": lab_summary,
            "leaderboard": flow.lab_leaderboard(),
        }


def main() -> None:
    payload = run_lab_in_a_box()
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not payload["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
