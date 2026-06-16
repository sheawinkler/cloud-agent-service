from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from cloud_agent_service.models import (
    DeploymentPolicy,
    JobRequest,
    RepoProvider,
    RoutingPolicy,
)

EVENT_SIGNATURE_HEADER = "x-agent-cloud-event-signature"
EVENT_ID_HEADER = "x-agent-cloud-event-id"

_SECRET_TEXT_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]+|AKIA[0-9A-Z]{16}|[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)=[^\s]+)"
)
_ABSOLUTE_PATH_RE = re.compile(r"(/Users/[^\s\"']+|/private/var/[^\s\"']+|/var/folders/[^\s\"']+)")
_SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|api[_-]?key|authorization)", re.I)


@dataclass(frozen=True)
class EventSignatureResult:
    ok: bool
    status: str
    mode: str
    missing: list[str]


@dataclass(frozen=True)
class EventIntakeRequest:
    source: str
    event_type: str
    idempotency_key: str
    intake_id: str
    run_immediately: bool
    payload: dict[str, Any]
    redacted_payload: dict[str, Any]


class EventIngestor:
    def __init__(self, secret: str | None = None) -> None:
        self.secret = secret if secret is not None else os.environ.get(
            "AGENT_CLOUD_EVENT_INGEST_SECRET",
            "",
        )

    def status(self) -> dict[str, Any]:
        configured = bool(self.secret)
        return {
            "provider": "generic-webhook",
            "configured": configured,
            "mode": "signed-hmac" if configured else "unsigned-local",
            "signature_header": EVENT_SIGNATURE_HEADER,
            "idempotency_header": EVENT_ID_HEADER,
            "missing": [] if configured else ["AGENT_CLOUD_EVENT_INGEST_SECRET"],
            "notes": [
                "When a secret is configured, /events/intake requires HMAC-SHA256 "
                "over the raw body.",
                "Unsigned local mode is for local demos only.",
            ],
        }

    def verify_signature(self, body: bytes, signature: str | None) -> EventSignatureResult:
        if not self.secret:
            return EventSignatureResult(
                ok=True,
                status="unsigned-local",
                mode="unsigned-local",
                missing=["AGENT_CLOUD_EVENT_INGEST_SECRET"],
            )
        expected = self.signature_for_body(body, self.secret)
        candidates = {expected, expected.removeprefix("sha256=")}
        ok = bool(signature) and signature.strip() in candidates
        return EventSignatureResult(
            ok=ok,
            status="verified" if ok else "rejected",
            mode="signed-hmac",
            missing=[] if ok else [EVENT_SIGNATURE_HEADER],
        )

    @staticmethod
    def signature_for_body(body: bytes, secret: str) -> str:
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return "sha256=" + digest

    def parse(
        self,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> EventIntakeRequest:
        headers = headers or {}
        source = str(payload.get("source") or headers.get("x-github-event") or "generic").strip()
        event_type = str(payload.get("event_type") or payload.get("action") or "event").strip()
        idempotency_key = self._idempotency_key(payload, headers, source, event_type)
        return EventIntakeRequest(
            source=source,
            event_type=event_type,
            idempotency_key=idempotency_key,
            intake_id="evt_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16],
            run_immediately=bool(payload.get("run_immediately", True)),
            payload=payload,
            redacted_payload=self.redact_payload(payload),
        )

    def to_job_request(self, intake: EventIntakeRequest) -> JobRequest | None:
        payload = intake.payload
        repo_provider = self._repo_provider(payload)
        repo_path = str(payload.get("repo_path") or "")
        git_url = payload.get("git_url")
        github_repo = payload.get("github_repo") or self._github_repo_from_payload(payload)
        if repo_provider == RepoProvider.LOCAL and not repo_path:
            return None
        if repo_provider == RepoProvider.GIT and not git_url:
            return None
        if repo_provider == RepoProvider.GITHUB and not github_repo:
            return None
        prompt = str(payload.get("prompt") or self._prompt_from_payload(intake))
        return JobRequest(
            prompt=prompt,
            repo_path=repo_path,
            repo_provider=repo_provider,
            git_url=str(git_url) if git_url else None,
            github_repo=str(github_repo) if github_repo else None,
            parent_job_id=payload.get("parent_job_id"),
            model_id=str(payload.get("model_id") or "local-deterministic"),
            agent_id=str(payload.get("agent_id") or "repo-editor-v1"),
            harness_id=str(payload.get("harness_id") or "local-template"),
            user_id=str(payload.get("user_id") or "event-user"),
            base_branch=str(payload.get("base_branch") or "main"),
            deploy_policy=DeploymentPolicy(str(payload.get("deploy_policy") or "manual")),
            routing_policy=RoutingPolicy(str(payload.get("routing_policy") or "fixed")),
            token_budget=int(payload.get("token_budget") or 8_000),
            max_prompt_chars=int(payload.get("max_prompt_chars") or 8_000),
            max_runtime_seconds=int(payload.get("max_runtime_seconds") or 600),
            max_changed_files=int(payload.get("max_changed_files") or 12),
        )

    @classmethod
    def redact_payload(cls, payload: Any) -> Any:
        if isinstance(payload, dict):
            redacted: dict[str, Any] = {}
            for key, value in payload.items():
                if _SENSITIVE_KEY_RE.search(str(key)):
                    redacted[key] = "<redacted_secret>"
                else:
                    redacted[key] = cls.redact_payload(value)
            return redacted
        if isinstance(payload, list):
            return [cls.redact_payload(item) for item in payload]
        if isinstance(payload, str):
            return _ABSOLUTE_PATH_RE.sub(
                "<redacted_path>",
                _SECRET_TEXT_RE.sub("<redacted_secret>", payload),
            )
        return payload

    @staticmethod
    def _repo_provider(payload: dict[str, Any]) -> RepoProvider:
        explicit = payload.get("repo_provider")
        if explicit:
            return RepoProvider(str(explicit))
        if payload.get("github_repo") or payload.get("repository", {}).get("full_name"):
            return RepoProvider.GITHUB
        if payload.get("git_url"):
            return RepoProvider.GIT
        return RepoProvider.LOCAL

    @staticmethod
    def _github_repo_from_payload(payload: dict[str, Any]) -> str | None:
        repository = payload.get("repository")
        if isinstance(repository, dict) and isinstance(repository.get("full_name"), str):
            return repository["full_name"]
        return None

    @staticmethod
    def _prompt_from_payload(intake: EventIntakeRequest) -> str:
        payload = intake.payload
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
        if issue:
            title = issue.get("title", "untitled issue")
            body = issue.get("body", "")
            number = issue.get("number", "")
            return f"Handle issue #{number}: {title}\n\n{body}".strip()
        if pr:
            title = pr.get("title", "untitled pull request")
            body = pr.get("body", "")
            number = pr.get("number", "")
            return f"Review pull request #{number}: {title}\n\n{body}".strip()
        compact = json.dumps(EventIngestor.redact_payload(payload), sort_keys=True)[:1200]
        return f"Handle {intake.source} {intake.event_type} event:\n{compact}"

    @staticmethod
    def _idempotency_key(
        payload: dict[str, Any],
        headers: dict[str, str],
        source: str,
        event_type: str,
    ) -> str:
        explicit = payload.get("idempotency_key") or headers.get(EVENT_ID_HEADER)
        if explicit:
            return str(explicit)
        stable = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        return f"{source}:{event_type}:{digest}"
