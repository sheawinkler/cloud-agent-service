from __future__ import annotations

import hashlib
import importlib
import os
from pathlib import Path

from cloud_agent_service.artifact_schema import RunArtifact
from cloud_agent_service.models import ArtifactReference


class ArtifactStorage:
    def __init__(
        self,
        *,
        provider: str = "local",
        artifacts_dir: str | Path,
        bucket: str = "",
        prefix: str = "cloud-agent-service",
        upload_enabled: bool = False,
    ) -> None:
        self.provider = provider
        self.artifacts_dir = Path(artifacts_dir)
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.upload_enabled = upload_enabled

    @classmethod
    def from_env(cls, artifacts_dir: str | Path) -> ArtifactStorage:
        provider = os.environ.get("AGENT_CLOUD_ARTIFACT_PROVIDER", "local").strip() or "local"
        return cls(
            provider=provider,
            artifacts_dir=artifacts_dir,
            bucket=os.environ.get("AGENT_CLOUD_ARTIFACT_BUCKET", "").strip(),
            prefix=os.environ.get("AGENT_CLOUD_ARTIFACT_PREFIX", "cloud-agent-service"),
            upload_enabled=_truthy(os.environ.get("AGENT_CLOUD_ARTIFACT_UPLOAD_ENABLED")),
        )

    def index_run_artifact(self, job_id: str, artifact: RunArtifact) -> list[ArtifactReference]:
        return [
            self._reference(job_id, "run_artifact", Path(artifact.artifact_path)),
            self._reference(job_id, "transcript", Path(artifact.transcript_path)),
            self._reference(job_id, "diff", Path(artifact.diff_path)),
        ]

    def _reference(self, job_id: str, artifact_type: str, path: Path) -> ArtifactReference:
        data = path.read_bytes() if path.exists() and path.is_file() else b""
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        uri = self._uri(job_id, artifact_type, path)
        if self.provider == "s3" and self.upload_enabled:
            self._upload_s3(path, uri)
        return ArtifactReference(
            job_id=job_id,
            artifact_type=artifact_type,
            provider=self.provider,
            uri=uri,
            path=str(path),
            sha256=digest,
            bytes=size,
        )

    def _uri(self, job_id: str, artifact_type: str, path: Path) -> str:
        if self.provider == "s3":
            if not self.bucket:
                raise ValueError("AGENT_CLOUD_ARTIFACT_BUCKET is required for s3 artifacts")
            return f"s3://{self.bucket}/{self._object_key(job_id, artifact_type, path)}"
        try:
            rel = path.relative_to(self.artifacts_dir)
        except ValueError:
            rel = path
        return f"local://artifacts/{rel.as_posix()}"

    def _object_key(self, job_id: str, artifact_type: str, path: Path) -> str:
        suffix = path.name or artifact_type
        return f"{self.prefix}/runs/{job_id}/{artifact_type}/{suffix}"

    def _upload_s3(self, path: Path, uri: str) -> None:
        if not path.exists() or not path.is_file():
            return
        boto3 = importlib.import_module("boto3")
        key = uri.split("/", 3)[3]
        boto3.client("s3").upload_file(str(path), self.bucket, key)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
