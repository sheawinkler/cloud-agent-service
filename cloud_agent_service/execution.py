from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionProviderStatus:
    provider: str
    configured: bool
    mode: str
    missing: list[str]
    notes: list[str]


class ExecutionProvider:
    def __init__(self, provider: str | None = None) -> None:
        selected = provider or os.environ.get("AGENT_CLOUD_EXECUTION_PROVIDER", "local")
        self.provider = selected.strip().lower().replace("-", "_") or "local"
        if self.provider not in {"local", "ecs_fargate", "vercel_sandbox"}:
            raise ValueError(f"unsupported execution provider: {self.provider}")

    def status(self) -> ExecutionProviderStatus:
        if self.provider == "local":
            return ExecutionProviderStatus(
                provider="local",
                configured=True,
                mode="local-container-contract",
                missing=[],
                notes=["Runs in the local service process or worker container."],
            )
        if self.provider == "ecs_fargate":
            missing = [
                name
                for name in (
                    "AGENT_CLOUD_ECS_CLUSTER",
                    "AGENT_CLOUD_ECS_TASK_DEFINITION",
                    "AGENT_CLOUD_ECS_SUBNETS",
                )
                if not os.environ.get(name)
            ]
            return ExecutionProviderStatus(
                provider="ecs_fargate",
                configured=not missing,
                mode="cloud-worker-dispatch",
                missing=missing,
                notes=["Submits workers through the env-gated ECS dispatch path."],
            )
        missing = []
        if _truthy(os.environ.get("AGENT_CLOUD_VERCEL_SANDBOX_ENABLED")) and not os.environ.get(
            "VERCEL_TOKEN"
        ):
            missing.append("VERCEL_TOKEN")
        return ExecutionProviderStatus(
            provider="vercel_sandbox",
            configured=not missing,
            mode=(
                "sandbox-contract"
                if not _truthy(os.environ.get("AGENT_CLOUD_VERCEL_SANDBOX_ENABLED"))
                else "sandbox-enabled"
            ),
            missing=missing,
            notes=[
                "Records Vercel Sandbox as an execution-provider contract; live sandbox "
                "execution requires a dedicated adapter."
            ],
        )

    def dispatch_event_payload(self) -> dict[str, Any]:
        status = self.status()
        return {
            "mode": status.mode,
            "provider": status.provider,
            "configured": status.configured,
        }


def execution_status_dict(provider: ExecutionProvider) -> dict[str, Any]:
    return asdict(provider.status())


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
