from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    VALIDATING = "validating"
    SYNCING = "syncing"
    DEPLOYING = "deploying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RiskLevel(StrEnum):
    DOCS_ONLY = "docs_only"
    CODE_EDIT = "code_edit"
    DEPENDENCY_CHANGE = "dependency_change"
    DEPLOYMENT = "deployment"
    INFRASTRUCTURE_CHANGE = "infrastructure_change"


class DeploymentPolicy(StrEnum):
    MANUAL = "manual"
    LOCAL = "local"
    NEVER = "never"


@dataclass(frozen=True)
class JobRequest:
    prompt: str
    repo_path: str
    user_id: str = "local-user"
    base_branch: str = "main"
    deploy_policy: DeploymentPolicy = DeploymentPolicy.MANUAL
    token_budget: int = 8_000
    max_prompt_chars: int = 8_000
    max_runtime_seconds: int = 600
    max_changed_files: int = 12


@dataclass
class NormalizedPrompt:
    brief: str
    acceptance_criteria: list[str]
    non_goals: list[str]
    risk_level: RiskLevel
    suggested_tests: list[str]
    ambiguities: list[str] = field(default_factory=list)


@dataclass
class AgentPlan:
    normalized_prompt: str
    acceptance_criteria: list[str]
    allowed_python_modules: list[str]
    allowed_shell_commands: list[str]
    disallowed_actions: list[str]
    expected_files_or_areas: list[str]
    required_tests: list[str]
    max_tokens: int
    max_runtime_seconds: int
    output_schema: dict[str, Any]


@dataclass
class JobResult:
    job_id: str
    status: JobStatus
    changed_files: list[str]
    commands_run: list[str]
    tests_passed: list[str]
    tests_failed: list[str]
    dependency_changes: list[str]
    policy_gate_results: dict[str, bool]
    pr_url: str | None
    deployment_status: str
    residual_risks: list[str]
    events: list[dict[str, Any]]
