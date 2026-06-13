from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

from cloud_agent_service.models import WorkerJobPayload


@dataclass(frozen=True)
class EcsDispatchConfig:
    cluster: str
    task_definition: str
    container_name: str
    subnets: list[str]
    security_groups: list[str]
    assign_public_ip: bool
    region: str
    launch_type: str = "FARGATE"


class EcsDispatchPlanner:
    REQUIRED_ENV = [
        "AGENT_CLOUD_ECS_CLUSTER",
        "AGENT_CLOUD_ECS_TASK_DEFINITION",
        "AGENT_CLOUD_ECS_SUBNETS",
    ]

    def status(self) -> dict[str, Any]:
        missing = [name for name in self.REQUIRED_ENV if not os.environ.get(name)]
        return {
            "configured": not missing,
            "provider": "aws-ecs",
            "mode": "dry-run-contract" if not missing else "local-only",
            "missing": missing,
        }

    def config_from_env(self) -> EcsDispatchConfig:
        status = self.status()
        if not status["configured"]:
            missing = ", ".join(status["missing"])
            raise ValueError(f"ECS dispatch is not configured; missing: {missing}")

        return EcsDispatchConfig(
            cluster=os.environ["AGENT_CLOUD_ECS_CLUSTER"],
            task_definition=os.environ["AGENT_CLOUD_ECS_TASK_DEFINITION"],
            container_name=os.environ.get("AGENT_CLOUD_ECS_CONTAINER_NAME", "agent"),
            subnets=_csv_env("AGENT_CLOUD_ECS_SUBNETS"),
            security_groups=_csv_env("AGENT_CLOUD_ECS_SECURITY_GROUPS"),
            assign_public_ip=_truthy(os.environ.get("AGENT_CLOUD_ECS_ASSIGN_PUBLIC_IP")),
            region=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
        )

    def build_run_task_request(self, payload: WorkerJobPayload) -> dict[str, Any]:
        config = self.config_from_env()
        return {
            "provider": "aws-ecs",
            "mode": "dry-run-contract",
            "region": config.region,
            "run_task_request": {
                "cluster": config.cluster,
                "taskDefinition": config.task_definition,
                "launchType": config.launch_type,
                "networkConfiguration": {
                    "awsvpcConfiguration": {
                        "subnets": config.subnets,
                        "securityGroups": config.security_groups,
                        "assignPublicIp": "ENABLED" if config.assign_public_ip else "DISABLED",
                    }
                },
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": config.container_name,
                            "command": [
                                "python",
                                "-m",
                                "cloud_agent_service.worker",
                                "--job-id",
                                payload.job_id,
                            ],
                            "environment": [
                                {"name": "AGENT_CLOUD_JOB_ID", "value": payload.job_id},
                                {"name": "AGENT_CLOUD_USER_ID", "value": payload.user_id},
                                {"name": "AGENT_CLOUD_MODEL_ID", "value": payload.model_id},
                                {"name": "AGENT_CLOUD_AGENT_ID", "value": payload.agent_id},
                                {"name": "AGENT_CLOUD_HARNESS_ID", "value": payload.harness_id},
                                {
                                    "name": "AGENT_CLOUD_STATUS_CALLBACK_URL",
                                    "value": payload.status_callback_url,
                                },
                            ],
                        }
                    ]
                },
                "tags": [
                    {"key": "job_id", "value": payload.job_id},
                    {"key": "user_id", "value": payload.user_id},
                    {"key": "model_id", "value": payload.model_id},
                    {"key": "agent_id", "value": payload.agent_id},
                    {"key": "harness_id", "value": payload.harness_id},
                ],
            },
            "worker_payload": asdict(payload),
        }


def _csv_env(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
