from __future__ import annotations

from dataclasses import dataclass

from cloud_agent_service.models import HarnessCategory, HarnessSpec


@dataclass(frozen=True)
class HarnessSecurityProfile:
    profile_id: str
    harness_id: str
    network_policy: str
    allowed_commands: list[str]
    readable_paths: list[str]
    writable_paths: list[str]
    secret_env_vars: list[str]
    max_runtime_seconds: int
    log_redaction_patterns: list[str]


class HarnessSecurityRegistry:
    def profile_for(
        self,
        harness: HarnessSpec,
        max_runtime_seconds: int,
    ) -> HarnessSecurityProfile:
        if harness.harness_id == "local-template":
            return HarnessSecurityProfile(
                profile_id="local-template.locked-down.v1",
                harness_id=harness.harness_id,
                network_policy="none",
                allowed_commands=["python3 -m compileall ."],
                readable_paths=["repo/**"],
                writable_paths=["repo/**", "artifacts/runs/**"],
                secret_env_vars=[],
                max_runtime_seconds=max_runtime_seconds,
                log_redaction_patterns=["sk-[A-Za-z0-9_-]+", "AKIA[0-9A-Z]{16}"],
            )
        if harness.harness_id == "pi-coding-agent":
            return HarnessSecurityProfile(
                profile_id="pi-coding-agent.cli-adapter.v1",
                harness_id=harness.harness_id,
                network_policy="egress-model-provider-only",
                allowed_commands=[
                    "pi-coding-agent",
                    "@earendil-works/pi-coding-agent",
                    "node",
                    "npx",
                ],
                readable_paths=["repo/**"],
                writable_paths=["repo/**", "artifacts/runs/**"],
                secret_env_vars=["PI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
                max_runtime_seconds=max_runtime_seconds,
                log_redaction_patterns=[
                    "sk-[A-Za-z0-9_-]+",
                    "PI_API_KEY=[^\\s]+",
                    "ANTHROPIC_API_KEY=[^\\s]+",
                    "OPENAI_API_KEY=[^\\s]+",
                ],
            )
        if harness.harness_id == "openai-codex-cli":
            return HarnessSecurityProfile(
                profile_id="openai-codex-cli.responses-edit-adapter.v1",
                harness_id=harness.harness_id,
                network_policy="egress-openai-only",
                allowed_commands=[],
                readable_paths=["repo/**"],
                writable_paths=["repo/**", "artifacts/runs/**"],
                secret_env_vars=["OPENAI_API_KEY"],
                max_runtime_seconds=max_runtime_seconds,
                log_redaction_patterns=[
                    "sk-[A-Za-z0-9_-]+",
                    "OPENAI_API_KEY=[^\\s]+",
                ],
            )
        if harness.category == HarnessCategory.CLOUD_CODING_AGENT:
            network_policy = "managed-provider-only"
        elif harness.category in {
            HarnessCategory.AGENT_SDK,
            HarnessCategory.ORCHESTRATION_RUNTIME,
        }:
            network_policy = "egress-model-provider-only"
        else:
            network_policy = "deny-by-default"
        return HarnessSecurityProfile(
            profile_id=f"{harness.harness_id}.contract-only.v1",
            harness_id=harness.harness_id,
            network_policy=network_policy,
            allowed_commands=[],
            readable_paths=["repo/**"],
            writable_paths=["repo/**", "artifacts/runs/**"],
            secret_env_vars=list(harness.env_requirements),
            max_runtime_seconds=max_runtime_seconds,
            log_redaction_patterns=["sk-[A-Za-z0-9_-]+", "AKIA[0-9A-Z]{16}"],
        )
