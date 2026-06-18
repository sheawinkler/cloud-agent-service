from __future__ import annotations

import hashlib
import importlib
import os
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from cloud_agent_service.models import CloudDispatchRecord, CloudDispatchStatus, WorkerJobPayload


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
        submit_enabled = _truthy(os.environ.get("AGENT_CLOUD_ECS_SUBMIT_ENABLED"))
        mode = "local-only"
        if not missing:
            mode = "live-submit" if submit_enabled else "dry-run-contract"
        return {
            "configured": not missing,
            "provider": "aws-ecs",
            "mode": mode,
            "missing": missing,
            "submit_enabled": submit_enabled,
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

    def build_run_task_request(
        self,
        payload: WorkerJobPayload,
        *,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        config = self.config_from_env()
        return self.build_run_task_request_for_config(
            payload,
            config,
            include_secrets=include_secrets,
        )

    def build_run_task_request_for_config(
        self,
        payload: WorkerJobPayload,
        config: EcsDispatchConfig,
        *,
        include_secrets: bool = False,
    ) -> dict[str, Any]:
        callback_env = []
        callback_token = payload.callback_auth.get("token")
        if callback_token:
            callback_env.append(
                {
                    "name": "AGENT_CLOUD_WORKER_CALLBACK_TOKEN",
                    "value": str(callback_token) if include_secrets else "<redacted>",
                }
            )
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
                                *callback_env,
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
            "worker_payload": self._redact_worker_payload(asdict(payload)),
        }

    def submit_run_task(
        self,
        payload: WorkerJobPayload,
        ecs_client: Any | None = None,
    ) -> CloudDispatchRecord:
        if not _truthy(os.environ.get("AGENT_CLOUD_ECS_SUBMIT_ENABLED")):
            raise ValueError("ECS submit is disabled; set AGENT_CLOUD_ECS_SUBMIT_ENABLED=1")
        plan = self.build_run_task_request(payload, include_secrets=True)
        request = plan["run_task_request"]
        region = plan["region"]
        client = ecs_client or self._ecs_client(region)
        try:
            response = client.run_task(**request)
        except Exception as exc:
            return CloudDispatchRecord(
                dispatch_id=self._dispatch_id(payload.job_id, request, {"error": str(exc)}),
                job_id=payload.job_id,
                provider="aws-ecs",
                mode="live-submit",
                status=CloudDispatchStatus.FAILED,
                task_arn=None,
                region=region,
                request=self._redact_request(request),
                response={"error": str(exc)},
            )
        tasks = response.get("tasks", []) if isinstance(response, dict) else []
        task_arn = tasks[0].get("taskArn") if tasks else None
        return CloudDispatchRecord(
            dispatch_id=self._dispatch_id(payload.job_id, request, response),
            job_id=payload.job_id,
            provider="aws-ecs",
            mode="live-submit",
            status=CloudDispatchStatus.SUBMITTED,
            task_arn=task_arn,
            region=region,
            request=self._redact_request(request),
            response=self._response_summary(response),
        )

    @staticmethod
    def _ecs_client(region: str) -> Any:
        boto3 = importlib.import_module("boto3")
        return boto3.client("ecs", region_name=region)

    @staticmethod
    def _response_summary(response: dict[str, Any]) -> dict[str, Any]:
        tasks = response.get("tasks", [])
        return {
            "task_arns": [task.get("taskArn") for task in tasks if task.get("taskArn")],
            "failures": response.get("failures", []),
            "response_metadata": response.get("ResponseMetadata", {}),
        }

    @staticmethod
    def _redact_worker_payload(payload: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(payload)
        callback_auth = dict(redacted.get("callback_auth") or {})
        if callback_auth.get("token"):
            callback_auth["token"] = "<redacted>"
        redacted["callback_auth"] = callback_auth
        return redacted

    @staticmethod
    def _redact_request(request: dict[str, Any]) -> dict[str, Any]:
        redacted = deepcopy(request)
        for override in (
            redacted.get("overrides", {}).get("containerOverrides", [])
            if isinstance(redacted.get("overrides"), dict)
            else []
        ):
            if not isinstance(override, dict):
                continue
            for entry in override.get("environment", []):
                if (
                    isinstance(entry, dict)
                    and entry.get("name") == "AGENT_CLOUD_WORKER_CALLBACK_TOKEN"
                ):
                    entry["value"] = "<redacted>"
        return redacted

    @staticmethod
    def _dispatch_id(
        job_id: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> str:
        seed = f"{job_id}|{request}|{response}"
        return "dispatch_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _csv_env(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
