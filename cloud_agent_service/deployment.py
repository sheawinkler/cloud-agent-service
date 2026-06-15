from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cloud_agent_service.models import DeploymentPolicy


@dataclass(frozen=True)
class DeploymentProviderStatus:
    provider: str
    configured: bool
    mode: str
    missing: list[str]
    live_submit_enabled: bool


@dataclass(frozen=True)
class DeploymentRecord:
    provider: str
    status: str
    url: str | None
    artifact_path: str | None
    metadata: dict[str, Any]


class DeploymentManager:
    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or os.environ.get(
            "AGENT_CLOUD_DEPLOYMENT_PROVIDER",
            "local_mock",
        )
        self.provider = self.provider.strip().lower().replace("-", "_") or "local_mock"
        if self.provider not in {"local_mock", "vercel_preview"}:
            raise ValueError(f"unsupported deployment provider: {self.provider}")

    def status(self) -> DeploymentProviderStatus:
        if self.provider == "local_mock":
            return DeploymentProviderStatus(
                provider="local_mock",
                configured=True,
                mode="local-artifact",
                missing=[],
                live_submit_enabled=True,
            )

        live_enabled = _truthy(os.environ.get("AGENT_CLOUD_VERCEL_DEPLOY_ENABLED"))
        missing = []
        if live_enabled and not shutil.which("vercel"):
            missing.append("vercel")
        if live_enabled and not os.environ.get("VERCEL_TOKEN"):
            missing.append("VERCEL_TOKEN")
        return DeploymentProviderStatus(
            provider="vercel_preview",
            configured=not missing,
            mode="live-preview" if live_enabled else "dry-run-contract",
            missing=missing,
            live_submit_enabled=live_enabled,
        )

    def deploy(
        self,
        *,
        repo_path: str | Path,
        artifacts_dir: str | Path,
        job_id: str,
        policy: DeploymentPolicy,
        evidence: dict[str, Any],
    ) -> DeploymentRecord:
        if self.provider == "vercel_preview":
            return self._deploy_vercel_preview(
                repo_path=Path(repo_path),
                artifacts_dir=Path(artifacts_dir),
                job_id=job_id,
                policy=policy,
                evidence=evidence,
            )
        return self._deploy_local(Path(artifacts_dir), job_id, policy)

    def _deploy_local(
        self,
        artifacts_dir: Path,
        job_id: str,
        policy: DeploymentPolicy,
    ) -> DeploymentRecord:
        status = _policy_status(policy)
        if not status.startswith("deployed:"):
            return DeploymentRecord(
                provider="local_mock",
                status=status,
                url=None,
                artifact_path=None,
                metadata={"policy": policy.value},
            )

        artifacts_dir.mkdir(parents=True, exist_ok=True)
        deploy_path = artifacts_dir / f"{job_id}-deployment.json"
        payload = {
            "provider": "local-deploy-mock",
            "job_id": job_id,
            "status": "deployed",
            "policy": policy.value,
        }
        deploy_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return DeploymentRecord(
            provider="local_mock",
            status=status,
            url=None,
            artifact_path=str(deploy_path),
            metadata=payload,
        )

    def _deploy_vercel_preview(
        self,
        *,
        repo_path: Path,
        artifacts_dir: Path,
        job_id: str,
        policy: DeploymentPolicy,
        evidence: dict[str, Any],
    ) -> DeploymentRecord:
        if policy in {DeploymentPolicy.NEVER, DeploymentPolicy.PR_ONLY}:
            return DeploymentRecord(
                provider="vercel_preview",
                status=_policy_status(policy),
                url=None,
                artifact_path=None,
                metadata={"policy": policy.value},
            )
        if policy in {DeploymentPolicy.MANUAL, DeploymentPolicy.PRODUCTION_APPROVAL}:
            return DeploymentRecord(
                provider="vercel_preview",
                status="ready: vercel preview approval required",
                url=None,
                artifact_path=None,
                metadata={"policy": policy.value},
            )

        artifacts_dir.mkdir(parents=True, exist_ok=True)
        contract_path = artifacts_dir / f"{job_id}-vercel-deployment.json"
        if not _truthy(os.environ.get("AGENT_CLOUD_VERCEL_DEPLOY_ENABLED")):
            payload = {
                "provider": "vercel_preview",
                "job_id": job_id,
                "mode": "dry-run-contract",
                "repo_path": str(repo_path),
                "policy": policy.value,
                "preview_url": evidence.get("preview_url"),
            }
            contract_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return DeploymentRecord(
                provider="vercel_preview",
                status="ready: vercel preview contract recorded",
                url=None,
                artifact_path=str(contract_path),
                metadata=payload,
            )

        command = ["vercel", "deploy", str(repo_path), "--yes"]
        if os.environ.get("VERCEL_TOKEN"):
            command.extend(["--token", os.environ["VERCEL_TOKEN"]])
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        stdout = completed.stdout.strip()
        url = _extract_vercel_url(stdout)
        payload = {
            "provider": "vercel_preview",
            "job_id": job_id,
            "mode": "live-preview",
            "command": ["vercel", "deploy", str(repo_path), "--yes"],
            "returncode": completed.returncode,
            "url": url,
            "stderr_tail": completed.stderr.strip()[-2000:],
        }
        contract_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        status = "deployed: vercel preview ready" if completed.returncode == 0 and url else (
            "not deployed: vercel preview failed"
        )
        return DeploymentRecord(
            provider="vercel_preview",
            status=status,
            url=url,
            artifact_path=str(contract_path),
            metadata=payload,
        )


def _policy_status(policy: DeploymentPolicy) -> str:
    if policy == DeploymentPolicy.NEVER:
        return "skipped: deployment disabled"
    if policy in {DeploymentPolicy.MANUAL, DeploymentPolicy.PRODUCTION_APPROVAL}:
        return "ready: manual approval required"
    if policy == DeploymentPolicy.PR_ONLY:
        return "skipped: PR only"
    if policy == DeploymentPolicy.PREVIEW_ONLY:
        return "ready: preview only"
    if policy == DeploymentPolicy.STAGING_AUTO:
        return "deployed: local staging mock deployment recorded"
    return "deployed: local mock deployment recorded"


def _extract_vercel_url(output: str) -> str | None:
    for token in output.split():
        if token.startswith("https://") and ".vercel.app" in token:
            return token.strip()
    return None


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def deployment_status_dict(manager: DeploymentManager) -> dict[str, Any]:
    return asdict(manager.status())
