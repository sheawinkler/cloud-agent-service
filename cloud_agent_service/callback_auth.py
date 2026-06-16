from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import asdict, dataclass

CALLBACK_TOKEN_HEADER = "x-agent-cloud-callback-token"


@dataclass(frozen=True)
class CallbackAuthStatus:
    configured: bool
    mode: str
    missing: list[str]
    header: str
    notes: list[str]


class WorkerCallbackAuth:
    def __init__(self, secret: str | None = None) -> None:
        self.secret = secret if secret is not None else os.environ.get(
            "AGENT_CLOUD_WORKER_CALLBACK_SECRET",
            "",
        )

    def status(self) -> CallbackAuthStatus:
        configured = bool(self.secret)
        return CallbackAuthStatus(
            configured=configured,
            mode="signed-hmac" if configured else "unsigned-local",
            missing=[] if configured else ["AGENT_CLOUD_WORKER_CALLBACK_SECRET"],
            header=CALLBACK_TOKEN_HEADER,
            notes=[
                (
                    "Worker callbacks require a job-scoped HMAC token when "
                    "AGENT_CLOUD_WORKER_CALLBACK_SECRET is configured."
                ),
                "Unsigned callbacks are accepted only for local/mock development.",
            ],
        )

    def status_dict(self) -> dict[str, object]:
        return asdict(self.status())

    def token_for_job(self, job_id: str) -> str:
        if not self.secret:
            return ""
        return hmac.new(
            self.secret.encode("utf-8"),
            job_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def payload_for_job(self, job_id: str) -> dict[str, object]:
        status = self.status()
        return {
            "mode": status.mode,
            "configured": status.configured,
            "header": status.header,
            "token": self.token_for_job(job_id),
        }

    def verify(self, job_id: str, token: str | None) -> bool:
        if not self.secret:
            return True
        expected = self.token_for_job(job_id)
        return bool(token) and hmac.compare_digest(expected, token)
