from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cloud_agent_service.analysis_lab import classify_failure
from cloud_agent_service.models import DatasetExport, JobResult, JobStatus
from cloud_agent_service.store import JobStore

SPLITS = ("train", "eval", "holdout")
ABSOLUTE_PATH_RE = re.compile(
    r"(/Users/[^\s\"']+|/private/var/[^\s\"']+|/var/folders/[^\s\"']+)"
)
GENERIC_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]+|AKIA[0-9A-Z]{16}|PI_API_KEY=[^\s]+|OPENAI_API_KEY=[^\s]+)"
)


class SlmDatasetExporter:
    schema_version = "slm-dataset.v1"

    def __init__(self, store: JobStore, artifacts_dir: str | Path) -> None:
        self.store = store
        self.artifacts_dir = Path(artifacts_dir)

    def export(
        self,
        *,
        export_id: str | None = None,
        limit: int = 200,
        promotion_status: str | None = None,
    ) -> DatasetExport:
        export_id = export_id or self._export_id(limit, promotion_status)
        export_dir = self.artifacts_dir / "datasets" / export_id
        export_dir.mkdir(parents=True, exist_ok=True)
        records_by_split: dict[str, list[dict[str, Any]]] = {split: [] for split in SPLITS}
        source_job_ids: list[str] = []
        for run in self.store.list_lab_runs(
            limit=limit,
            promotion_status=promotion_status,
        ):
            job = self.store.get_job(run["job_id"])
            if not job or not job.get("result_json"):
                continue
            result = self._stored_result(job)
            artifact = result.evidence.get("run_artifact", {})
            if artifact.get("complete") is not True:
                continue
            record = self._record_from_result(job, result)
            split = self._split_for_job(result.job_id)
            records_by_split[split].append(record)
            source_job_ids.append(result.job_id)

        split_paths: dict[str, str] = {}
        counts: dict[str, int] = {}
        for split, records in records_by_split.items():
            path = export_dir / f"{split}.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, sort_keys=True) + "\n")
            split_paths[split] = str(path)
            counts[split] = len(records)

        manifest_path = export_dir / "manifest.json"
        manifest = DatasetExport(
            export_id=export_id,
            artifact_path=str(manifest_path),
            split_paths=split_paths,
            counts=counts,
            source_job_ids=source_job_ids,
        )
        manifest_path.write_text(
            json.dumps(asdict(manifest), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        self.store.upsert_dataset_export(asdict(manifest))
        return manifest

    def _record_from_result(self, job: dict[str, Any], result: JobResult) -> dict[str, Any]:
        evidence = result.evidence
        adapter = evidence.get("harness_adapter_result", {})
        transcript = self._redact_text("\n".join(adapter.get("transcript", [])))[:4_000]
        artifact = evidence.get("run_artifact", {})
        diff_path = Path(artifact.get("diff_path", ""))
        diff_fingerprint = self._diff_fingerprint(diff_path)
        promotion = result.promotion_decision.get("status", "unknown")
        return {
            "schema_version": self.schema_version,
            "job_id": result.job_id,
            "prompt": self._redact_text(job["prompt"]),
            "normalized_prompt": self._redact_text(job.get("normalized_prompt", "")),
            "repo_provider": job["repo_provider"],
            "repo_profile": self._redact_json(evidence.get("repo_profile", {})),
            "model_id": job["model_id"],
            "agent_id": job["agent_id"],
            "harness_id": job["harness_id"],
            "adapter_id": adapter.get("adapter_id"),
            "adapter_status": adapter.get("adapter_status"),
            "changed_files": result.changed_files,
            "commands_run": [self._redact_text(command) for command in result.commands_run],
            "tests_passed": result.tests_passed,
            "tests_failed": result.tests_failed,
            "policy_gate_results": result.policy_gate_results,
            "promotion_status": promotion,
            "failure_category": classify_failure(result),
            "run_artifact_complete": artifact.get("complete") is True,
            "transcript_excerpt": transcript,
            "diff_fingerprint": diff_fingerprint,
        }

    def _redact_json(self, value: Any) -> Any:
        return json.loads(self._redact_text(json.dumps(value, sort_keys=True)))

    @staticmethod
    def _stored_result(job: dict[str, Any]) -> JobResult:
        result = job["result_json"]
        return JobResult(
            job_id=result["job_id"],
            status=JobStatus(result["status"]),
            changed_files=result["changed_files"],
            commands_run=result["commands_run"],
            tests_passed=result["tests_passed"],
            tests_failed=result["tests_failed"],
            dependency_changes=result["dependency_changes"],
            policy_gate_results=result["policy_gate_results"],
            pr_url=result["pr_url"],
            deployment_status=result["deployment_status"],
            residual_risks=result["residual_risks"],
            events=[],
            evidence=result.get("evidence", {}),
            promotion_decision=result.get("promotion_decision", {}),
        )

    @staticmethod
    def _split_for_job(job_id: str) -> str:
        bucket = int(hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        if bucket < 80:
            return "train"
        if bucket < 90:
            return "eval"
        return "holdout"

    @staticmethod
    def _export_id(limit: int, promotion_status: str | None) -> str:
        seed = f"{limit}|{promotion_status or 'all'}"
        return "ds_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _redact_text(text: str) -> str:
        text = ABSOLUTE_PATH_RE.sub("<redacted_path>", text)
        return GENERIC_SECRET_RE.sub("<redacted_secret>", text)

    @staticmethod
    def _diff_fingerprint(path: Path) -> dict[str, Any]:
        if not path.exists() or not path.is_file():
            return {"available": False, "sha256": None, "bytes": 0}
        data = path.read_bytes()
        return {
            "available": True,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
