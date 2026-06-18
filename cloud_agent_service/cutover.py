from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from cloud_agent_service.callback_auth import (
    CALLBACK_TOKEN_HEADER,
    WorkerCallbackAuth,
)
from cloud_agent_service.cloud_dispatch import EcsDispatchConfig, EcsDispatchPlanner
from cloud_agent_service.event_ingest import EVENT_SIGNATURE_HEADER, EventIngestor
from cloud_agent_service.models import DeploymentPolicy, JobRequest
from cloud_agent_service.readiness import ReadinessReporter

DEFAULT_CUTOVER_PROMPT = "For my shopping website, create a buy button."
DEFAULT_STATUS_CALLBACK_URL = "https://api.example.com/jobs"


@dataclass(frozen=True)
class CutoverCheck:
    check_id: str
    title: str
    ok: bool
    critical: bool
    evidence: dict[str, Any]


class CutoverRehearsal:
    schema_version = "cutover-rehearsal.v1"
    status_schema_version = "cutover-status.v1"

    def __init__(
        self,
        flow: Any,
        ecs_dispatch_planner: EcsDispatchPlanner,
        *,
        event_ingestor: EventIngestor | None = None,
        callback_secret: str = "cutover-rehearsal-callback-secret",
        event_secret: str = "cutover-rehearsal-event-secret",
    ) -> None:
        self.flow = flow
        self.ecs_dispatch_planner = ecs_dispatch_planner
        self.event_ingestor = event_ingestor or EventIngestor()
        self.callback_auth = WorkerCallbackAuth(secret=callback_secret)
        self.signed_event_ingestor = EventIngestor(secret=event_secret)

    def status(self) -> dict[str, Any]:
        readiness = self._readiness()
        return {
            "schema_version": self.status_schema_version,
            "production_ready": readiness["production_ready"],
            "readiness_score": readiness["readiness_score"],
            "status_counts": readiness["status_counts"],
            "critical_blockers": readiness["critical_blockers"],
            "operator_next_steps": self._operator_next_steps(readiness),
            "rehearsal": {
                "endpoint": "POST /cutover/rehearse",
                "script": "python3 scripts/rehearse_cutover.py",
                "default_live_external_calls": False,
                "proofs": [
                    "queued job and worker payload",
                    "job-scoped worker callback HMAC",
                    "signed event intake HMAC",
                    "redacted ECS/Fargate run_task dry-run request",
                    "readiness scorecard production blocker binding",
                ],
            },
            "integrations": self._integrations(),
        }

    def rehearse(
        self,
        *,
        repo_path: str,
        prompt: str = DEFAULT_CUTOVER_PROMPT,
        user_id: str = "cutover-rehearsal-user",
        status_callback_url: str = DEFAULT_STATUS_CALLBACK_URL,
    ) -> dict[str, Any]:
        job_id = self.flow.create_job(
            JobRequest(
                prompt=prompt,
                repo_path=repo_path,
                user_id=user_id,
                deploy_policy=DeploymentPolicy.MANUAL,
                token_budget=2_000,
                max_changed_files=2,
            )
        )
        payload = self.flow.build_worker_payload(
            job_id,
            status_callback_url=status_callback_url,
        )
        signed_payload = replace(
            payload,
            callback_auth=self.callback_auth.payload_for_job(job_id),
        )
        ecs_config, ecs_plan_source = self._ecs_config()
        ecs_plan = self.ecs_dispatch_planner.build_run_task_request_for_config(
            signed_payload,
            ecs_config,
        )
        redacted_ecs_plan = self._redact_rehearsal_payload(ecs_plan)
        callback_proof = self._callback_proof(job_id)
        event_proof = self._event_proof(prompt=prompt, repo_path=repo_path)
        readiness = self._readiness()
        checks = self._checks(
            job_id=job_id,
            payload=signed_payload,
            ecs_plan=redacted_ecs_plan,
            ecs_plan_source=ecs_plan_source,
            callback_proof=callback_proof,
            event_proof=event_proof,
            readiness=readiness,
        )
        ok = all(check.ok for check in checks if check.critical)
        report = {
            "schema_version": self.schema_version,
            "rehearsal_id": self._rehearsal_id(job_id, prompt, repo_path),
            "created_at": datetime.now(UTC).isoformat(),
            "ok": ok,
            "job_id": job_id,
            "repo_provider": payload.repo_provider,
            "status_callback_url": signed_payload.status_callback_url,
            "live_external_calls_made": False,
            "checks": [asdict(check) for check in checks],
            "proofs": {
                "worker_payload": {
                    "job_id": signed_payload.job_id,
                    "model_id": signed_payload.model_id,
                    "agent_id": signed_payload.agent_id,
                    "harness_id": signed_payload.harness_id,
                    "working_branch": signed_payload.working_branch,
                    "callback_auth": self._redacted_callback_auth(
                        signed_payload.callback_auth
                    ),
                },
                "worker_callback_hmac": callback_proof,
                "event_intake_hmac": event_proof,
                "ecs_dry_run": {
                    "plan_source": ecs_plan_source,
                    "provider": redacted_ecs_plan["provider"],
                    "mode": redacted_ecs_plan["mode"],
                    "region": redacted_ecs_plan["region"],
                    "run_task_request": redacted_ecs_plan["run_task_request"],
                    "worker_payload": redacted_ecs_plan["worker_payload"],
                    "live_submit_attempted": False,
                },
            },
            "readiness": {
                "schema_version": readiness["schema_version"],
                "readiness_score": readiness["readiness_score"],
                "production_ready": readiness["production_ready"],
                "status_counts": readiness["status_counts"],
                "critical_blockers": readiness["critical_blockers"],
                "operator_next_steps": self._operator_next_steps(readiness),
            },
            "cutover_decision": self._cutover_decision(readiness, ok),
        }
        self.flow.store.add_event(
            job_id,
            "cutover_rehearsal_created",
            {
                "rehearsal_id": report["rehearsal_id"],
                "ok": ok,
                "production_ready": readiness["production_ready"],
                "critical_blockers": readiness["critical_blockers"],
            },
        )
        return report

    def _checks(
        self,
        *,
        job_id: str,
        payload: Any,
        ecs_plan: dict[str, Any],
        ecs_plan_source: str,
        callback_proof: dict[str, Any],
        event_proof: dict[str, Any],
        readiness: dict[str, Any],
    ) -> list[CutoverCheck]:
        request = ecs_plan["run_task_request"]
        container = request["overrides"]["containerOverrides"][0]
        env = {entry["name"]: entry["value"] for entry in container["environment"]}
        return [
            CutoverCheck(
                "queued-job-worker-payload",
                "queued job and worker payload are buildable",
                bool(job_id and payload.job_id == job_id and payload.working_branch),
                True,
                {
                    "job_id": job_id,
                    "working_branch": payload.working_branch,
                    "model_id": payload.model_id,
                    "agent_id": payload.agent_id,
                    "harness_id": payload.harness_id,
                },
            ),
            CutoverCheck(
                "worker-callback-hmac",
                "worker callback token verifies and rejects wrong tokens",
                bool(callback_proof["valid_token_verified"])
                and bool(callback_proof["wrong_token_rejected"]),
                True,
                callback_proof,
            ),
            CutoverCheck(
                "event-intake-hmac",
                "event intake signature verifies and rejects wrong signatures",
                bool(event_proof["valid_signature_verified"])
                and bool(event_proof["wrong_signature_rejected"]),
                True,
                event_proof,
            ),
            CutoverCheck(
                "ecs-dry-run-redacted",
                "ECS dry-run request is buildable and redacts callback token",
                self._ecs_cluster_matches_source(
                    str(request["cluster"]),
                    ecs_plan_source,
                )
                and env.get("AGENT_CLOUD_WORKER_CALLBACK_TOKEN") == "<redacted>"
                and ecs_plan["worker_payload"]["callback_auth"]["token"] == "<redacted>",
                True,
                {
                    "plan_source": ecs_plan_source,
                    "cluster": request["cluster"],
                    "task_definition": request["taskDefinition"],
                    "container": container["name"],
                    "callback_token_env": env.get("AGENT_CLOUD_WORKER_CALLBACK_TOKEN"),
                    "live_submit_attempted": False,
                },
            ),
            CutoverCheck(
                "readiness-blockers-bound",
                "production readiness is bound to critical blockers",
                readiness["production_ready"] is (not readiness["critical_blockers"]),
                True,
                {
                    "production_ready": readiness["production_ready"],
                    "critical_blockers": readiness["critical_blockers"],
                    "readiness_score": readiness["readiness_score"],
                },
            ),
        ]

    def _callback_proof(self, job_id: str) -> dict[str, Any]:
        token = self.callback_auth.token_for_job(job_id)
        return {
            "mode": "signed-hmac",
            "header": CALLBACK_TOKEN_HEADER,
            "configured_for_rehearsal": True,
            "token": "<redacted>",
            "valid_token_verified": self.callback_auth.verify(job_id, token),
            "wrong_token_rejected": not self.callback_auth.verify(job_id, "wrong-token"),
            "live_callback_auth_configured": self.flow.callback_auth_status()["configured"],
        }

    def _event_proof(self, *, prompt: str, repo_path: str) -> dict[str, Any]:
        payload = {
            "source": "github",
            "event_type": "issues",
            "idempotency_key": "cutover-rehearsal-event",
            "prompt": prompt,
            "repo_path": repo_path,
            "run_immediately": False,
            "secret_marker": "OPENAI_API_KEY=test-secret",
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = self.signed_event_ingestor.signature_for_body(
            body,
            str(self.signed_event_ingestor.secret),
        )
        valid = self.signed_event_ingestor.verify_signature(body, signature)
        invalid = self.signed_event_ingestor.verify_signature(body, "sha256=bad")
        return {
            "mode": "signed-hmac",
            "header": EVENT_SIGNATURE_HEADER,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "signature": "<redacted>",
            "valid_signature_verified": valid.ok,
            "wrong_signature_rejected": not invalid.ok,
            "redacted_payload": EventIngestor.redact_payload(payload),
            "live_event_ingest_configured": self.event_ingestor.status()["configured"],
        }

    def _ecs_config(self) -> tuple[EcsDispatchConfig, str]:
        status = self.ecs_dispatch_planner.status()
        if status["configured"]:
            try:
                return self.ecs_dispatch_planner.config_from_env(), "configured-env"
            except ValueError:
                pass
        return (
            EcsDispatchConfig(
                cluster="agent-cloud-rehearsal",
                task_definition="agent-cloud-worker:rehearsal",
                container_name="agent",
                subnets=["subnet-rehearsal-a", "subnet-rehearsal-b"],
                security_groups=["sg-rehearsal"],
                assign_public_ip=False,
                region="us-east-1",
            ),
            "synthetic-local-contract",
        )

    @staticmethod
    def _ecs_cluster_matches_source(cluster: str, plan_source: str) -> bool:
        if plan_source == "synthetic-local-contract":
            return cluster == "agent-cloud-rehearsal"
        return bool(cluster)

    @staticmethod
    def _redacted_callback_auth(callback_auth: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(callback_auth)
        if redacted.get("token"):
            redacted["token"] = "<redacted>"
        return redacted

    @classmethod
    def _redact_rehearsal_payload(cls, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in {
                    "authorization",
                    "password",
                    "secret",
                    "token",
                    "api_key",
                    "api-key",
                }:
                    redacted[key] = "<redacted>" if item == "<redacted>" else "<redacted_secret>"
                else:
                    redacted[key] = cls._redact_rehearsal_payload(item)
            return redacted
        if isinstance(value, list):
            return [cls._redact_rehearsal_payload(item) for item in value]
        if isinstance(value, str):
            return EventIngestor.redact_payload(value)
        return value

    def _readiness(self) -> dict[str, Any]:
        return ReadinessReporter(self.flow, self.ecs_dispatch_planner).report()

    def _integrations(self) -> dict[str, Any]:
        return {
            "cloud": self.ecs_dispatch_planner.status(),
            "callback_auth": self.flow.callback_auth_status(),
            "events": self.event_ingestor.status(),
            "database": self.flow.database_status(),
            "deployment": asdict(self.flow.deployer.status()),
            "execution": asdict(self.flow.execution_provider.status()),
            "forge": self.flow.forge_status(),
            "github": asdict(self.flow.github_status()),
        }

    @staticmethod
    def _operator_next_steps(readiness: dict[str, Any]) -> list[str]:
        blockers = set(readiness["critical_blockers"])
        steps: list[str] = []
        for capability in readiness["capabilities"]:
            if capability["capability_id"] not in blockers:
                continue
            for step in capability.get("next_steps", []):
                if step not in steps:
                    steps.append(step)
        return steps

    @staticmethod
    def _cutover_decision(readiness: dict[str, Any], ok: bool) -> dict[str, Any]:
        if not ok:
            return {
                "status": "rehearsal_failed",
                "reason": "one or more critical local cutover proofs failed",
                "production_deploy_approved": False,
            }
        if readiness["production_ready"]:
            return {
                "status": "ready_for_operator_cutover",
                "reason": (
                    "all critical readiness blockers are clear; operator approval still required"
                ),
                "production_deploy_approved": False,
            }
        return {
            "status": "blocked_for_production",
            "reason": "local rehearsal passed but critical production blockers remain",
            "critical_blockers": readiness["critical_blockers"],
            "production_deploy_approved": False,
        }

    @staticmethod
    def _rehearsal_id(job_id: str, prompt: str, repo_path: str) -> str:
        seed = f"{job_id}|{prompt}|{repo_path}|cutover-rehearsal.v1"
        return "cutover_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
