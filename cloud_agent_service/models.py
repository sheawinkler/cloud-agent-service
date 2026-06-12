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
    CANCELLED = "cancelled"
    NEEDS_USER_INPUT = "needs_user_input"


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
    PR_ONLY = "pr_only"
    PREVIEW_ONLY = "preview_only"
    STAGING_AUTO = "staging_auto"
    PRODUCTION_APPROVAL = "production_approval"


class RepoProvider(StrEnum):
    LOCAL = "local"
    GIT = "git"
    GITHUB = "github"


class PromotionStatus(StrEnum):
    PROMOTE = "promote"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class TaskCase:
    task_id: str
    prompt: str
    deploy_policy: DeploymentPolicy = DeploymentPolicy.MANUAL
    expected_job_status: JobStatus = JobStatus.SUCCEEDED
    expected_promotion_status: PromotionStatus = PromotionStatus.NEEDS_REVIEW
    expected_changed_files: list[str] = field(default_factory=list)
    token_budget: int = 8_000
    max_changed_files: int = 12


@dataclass(frozen=True)
class TaskSuite:
    suite_id: str
    cases: list[TaskCase]


@dataclass(frozen=True)
class JobRequest:
    prompt: str
    repo_path: str = ""
    repo_provider: RepoProvider = RepoProvider.LOCAL
    git_url: str | None = None
    github_repo: str | None = None
    parent_job_id: str | None = None
    model_id: str = "local-deterministic"
    agent_id: str = "repo-editor-v1"
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
    evidence: dict[str, Any] = field(default_factory=dict)
    promotion_decision: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerJobPayload:
    job_id: str
    user_id: str
    repo_provider: str
    repo_path: str
    git_url: str | None
    github_repo: str | None
    base_branch: str
    working_branch: str
    parent_job_id: str | None
    model_id: str
    agent_id: str
    model_spec: dict[str, Any]
    agent_spec: dict[str, Any]
    normalized_prompt: str
    acceptance_criteria: list[str]
    allowed_python_modules: list[str]
    allowed_shell_commands: list[str]
    token_budget: int
    max_runtime_seconds: int
    max_changed_files: int
    deployment_policy: DeploymentPolicy
    status_callback_url: str
    output_schema: dict[str, Any]


@dataclass
class RepoProfile:
    package_manager: str | None
    framework: str | None
    detected_files: list[str]
    suggested_test_commands: list[str]
    risk_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    provider: str
    name: str
    context_window: int
    cost_tier: str
    supports_tools: bool


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    role: str
    model_id: str
    allowed_shell_commands: list[str]
    output_contract: str


@dataclass
class PromotionDecision:
    status: PromotionStatus
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class GitHubIntegrationStatus:
    configured: bool
    provider: str
    missing: list[str]
    mode: str


@dataclass
class PreviewArtifact:
    preview_url: str | None
    artifact_path: str | None
    browser_proof_path: str | None
    checks: dict[str, bool] = field(default_factory=dict)
