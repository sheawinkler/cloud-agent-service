from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProvenanceRecord:
    schema_version: str
    job_id: str
    path: str
    sha256: str
    bytes: int
    manifest: dict[str, Any]


class ProvenanceWriter:
    schema_version = "provenance-manifest.v1"

    def write(
        self,
        *,
        artifacts_dir: str | Path,
        job_id: str,
        repo_path: str | Path,
        changed_files: list[str],
        evidence: dict[str, Any],
        policy_gates: dict[str, bool],
        deployment_record: dict[str, Any],
    ) -> ProvenanceRecord:
        artifacts = Path(artifacts_dir) / "provenance" / job_id
        artifacts.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": self.schema_version,
            "job_id": job_id,
            "changed_files": changed_files,
            "source_fingerprints": self._source_fingerprints(Path(repo_path), changed_files),
            "run_artifact": evidence.get("run_artifact"),
            "artifact_refs": evidence.get("artifact_refs", []),
            "deployment": deployment_record,
            "policy_gates": policy_gates,
            "promotion_input": {
                "preview_url": evidence.get("preview_url"),
                "routing_policy": evidence.get("routing_policy"),
                "model_id": evidence.get("model_spec", {}).get("model_id"),
                "agent_id": evidence.get("agent_spec", {}).get("agent_id"),
                "harness_id": evidence.get("harness_spec", {}).get("harness_id"),
            },
        }
        path = artifacts / "manifest.json"
        payload = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(payload)
        return ProvenanceRecord(
            schema_version=self.schema_version,
            job_id=job_id,
            path=str(path),
            sha256=hashlib.sha256(payload).hexdigest(),
            bytes=len(payload),
            manifest=manifest,
        )

    @staticmethod
    def _source_fingerprints(repo_path: Path, changed_files: list[str]) -> list[dict[str, Any]]:
        fingerprints = []
        for rel_path in sorted(changed_files):
            path = repo_path / rel_path
            if not path.exists() or not path.is_file():
                fingerprints.append(
                    {
                        "path": rel_path,
                        "available": False,
                        "sha256": None,
                        "bytes": 0,
                    }
                )
                continue
            data = path.read_bytes()
            fingerprints.append(
                {
                    "path": rel_path,
                    "available": True,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
        return fingerprints
