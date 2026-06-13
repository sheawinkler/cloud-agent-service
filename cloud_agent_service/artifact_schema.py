from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloud_agent_service.harness_adapters import HarnessExecutionResult
from cloud_agent_service.security_profiles import HarnessSecurityProfile


@dataclass(frozen=True)
class RunArtifact:
    schema_version: str
    artifact_path: str
    transcript_path: str
    diff_path: str
    complete: bool
    policy_gate_results: dict[str, bool]


class RunArtifactWriter:
    schema_version = "run-artifact.v1"

    def write(
        self,
        artifacts_dir: str | Path,
        job_id: str,
        repo_path: str | Path,
        prompt: str,
        normalized_prompt: str,
        repo_provider: str,
        model_id: str,
        agent_id: str,
        harness_id: str,
        adapter_result: HarnessExecutionResult,
        security_profile: HarnessSecurityProfile,
        base_policy_gates: dict[str, bool],
        preview: dict[str, Any],
    ) -> RunArtifact:
        run_dir = Path(artifacts_dir) / "runs" / job_id
        run_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = run_dir / "transcript.txt"
        diff_path = run_dir / "diff.patch"
        artifact_path = run_dir / "run-artifact.json"
        transcript_path.write_text(
            "\n".join(adapter_result.transcript).rstrip() + "\n",
            encoding="utf-8",
        )
        diff_path.write_text(
            self._diff_text(Path(repo_path), adapter_result.changed_files),
            encoding="utf-8",
        )
        final_gates = {
            **base_policy_gates,
            "artifact_policy": True,
            "transcript_policy": transcript_path.exists() and transcript_path.stat().st_size > 0,
            "security_profile_policy": bool(security_profile.profile_id),
        }
        complete = all(
            [
                final_gates["artifact_policy"],
                final_gates["transcript_policy"],
                final_gates["security_profile_policy"],
                diff_path.exists(),
            ]
        )
        payload = {
            "schema_version": self.schema_version,
            "job_id": job_id,
            "repo": {
                "provider": repo_provider,
                "path": str(repo_path),
                "revision": self._repo_revision(Path(repo_path)),
            },
            "lab": {
                "model_id": model_id,
                "agent_id": agent_id,
                "harness_id": harness_id,
                "adapter_id": adapter_result.adapter_id,
                "adapter_status": adapter_result.adapter_status,
            },
            "prompt": prompt,
            "normalized_prompt": normalized_prompt,
            "changed_files": adapter_result.changed_files,
            "commands_run": adapter_result.commands_run,
            "tests_passed": adapter_result.tests_passed,
            "tests_failed": adapter_result.tests_failed,
            "dependency_changes": adapter_result.dependency_changes,
            "policy_gate_results": final_gates,
            "security_profile": security_profile.__dict__,
            "preview": preview,
            "artifacts": {
                "transcript_path": str(transcript_path),
                "diff_path": str(diff_path),
            },
            "complete": complete,
        }
        artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return RunArtifact(
            schema_version=self.schema_version,
            artifact_path=str(artifact_path),
            transcript_path=str(transcript_path),
            diff_path=str(diff_path),
            complete=complete,
            policy_gate_results=final_gates,
        )

    @staticmethod
    def _repo_revision(repo: Path) -> str:
        if not (repo / ".git").exists():
            return "workspace-no-git"
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "git-revision-unavailable"

    @staticmethod
    def _diff_text(repo: Path, changed_files: list[str]) -> str:
        if (repo / ".git").exists():
            result = subprocess.run(
                ["git", "diff", "--", *changed_files],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.stdout:
                return result.stdout
        lines = ["# Diff unavailable: workspace has no git baseline.", ""]
        for rel_path in changed_files:
            path = repo / rel_path
            if path.exists() and path.is_file():
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                lines.append(f"changed {rel_path} sha256={digest} bytes={len(data)}")
            else:
                lines.append(f"changed {rel_path} missing_after_run=true")
        return "\n".join(lines).rstrip() + "\n"
