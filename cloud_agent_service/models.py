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


class RoutingPolicy(StrEnum):
    FIXED = "fixed"
    RECOMMEND_ONLY = "recommend_only"
    AUTO_SELECT = "auto_select"


class CloudDispatchStatus(StrEnum):
    PLANNED = "planned"
    SUBMITTED = "submitted"
    FAILED = "failed"


class WorkerCallbackType(StrEnum):
    STARTED = "started"
    HEARTBEAT = "heartbeat"
    ARTIFACT_UPLOADED = "artifact_uploaded"
    COMPLETED = "completed"
    FAILED = "failed"


class HarnessCategory(StrEnum):
    CODING_CLI = "coding_cli"
    CLOUD_CODING_AGENT = "cloud_coding_agent"
    AGENT_SDK = "agent_sdk"
    ORCHESTRATION_RUNTIME = "orchestration_runtime"
    CUSTOM = "custom"


@dataclass(frozen=True)
class HarnessSpec:
    harness_id: str
    name: str
    category: HarnessCategory
    rank: int | None
    provider: str
    runtime: str
    execution_contract: str
    repo_url: str | None
    docs_url: str
    install_hint: str
    env_requirements: list[str]
    strengths: list[str]
    integration_notes: list[str]


@dataclass(frozen=True)
class TaskCase:
    task_id: str
    prompt: str
    deploy_policy: DeploymentPolicy = DeploymentPolicy.MANUAL
    expected_job_status: JobStatus = JobStatus.SUCCEEDED
    expected_promotion_status: PromotionStatus = PromotionStatus.NEEDS_REVIEW
    expected_changed_files: list[str] = field(default_factory=list)
    harness_id: str = "local-template"
    token_budget: int = 8_000
    max_changed_files: int = 12


@dataclass(frozen=True)
class TaskSuite:
    suite_id: str
    cases: list[TaskCase]


@dataclass(frozen=True)
class AnalysisCase:
    case_id: str
    title: str
    category: str
    prompt: str
    task_ids: list[str]
    model_ids: list[str]
    agent_ids: list[str]
    harness_ids: list[str]
    success_criteria: list[str]
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    case_id: str
    name: str
    model_ids: list[str]
    agent_ids: list[str]
    harness_ids: list[str]
    task_ids: list[str]
    notes: str = ""


@dataclass(frozen=True)
class RunAnalysis:
    job_id: str
    case_id: str
    model_id: str
    agent_id: str
    harness_id: str
    job_status: str
    promotion_status: str
    failure_category: str
    evaluator_notes: str
    changed_files_count: int
    tests_failed_count: int
    token_budget: int
    tokens_used: int
    run_artifact_complete: bool


@dataclass(frozen=True)
class ExperimentRun:
    experiment_id: str
    job_ids: list[str]
    analyses: list[RunAnalysis]


@dataclass(frozen=True)
class ExperimentReport:
    experiment_id: str
    case_id: str
    total_runs: int
    by_promotion_status: dict[str, int]
    failure_categories: dict[str, int]
    best_candidates: list[dict[str, Any]]
    runs: list[RunAnalysis]


@dataclass(frozen=True)
class DatasetExport:
    export_id: str
    artifact_path: str
    split_paths: dict[str, str]
    counts: dict[str, int]
    source_job_ids: list[str]
    lineage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExperimentBatch:
    batch_id: str
    experiment_id: str
    status: str
    max_concurrency: int
    requested_jobs: int
    completed_jobs: int
    failed_jobs: int
    job_ids: list[str]


@dataclass(frozen=True)
class CloudDispatchRecord:
    dispatch_id: str
    job_id: str
    provider: str
    mode: str
    status: CloudDispatchStatus
    task_arn: str | None
    region: str
    request: dict[str, Any]
    response: dict[str, Any]


@dataclass(frozen=True)
class WorkerCallbackRecord:
    job_id: str
    callback_type: WorkerCallbackType
    status: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ArtifactReference:
    job_id: str
    artifact_type: str
    provider: str
    uri: str
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class RoutingDecision:
    routing_policy: RoutingPolicy
    selected_model_id: str
    selected_agent_id: str
    selected_harness_id: str
    confidence: float
    reason: str
    nearest_analysis_cases: list[str]
    fallback: bool


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
    harness_id: str = "local-template"
    user_id: str = "local-user"
    base_branch: str = "main"
    deploy_policy: DeploymentPolicy = DeploymentPolicy.MANUAL
    routing_policy: RoutingPolicy = RoutingPolicy.FIXED
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
    harness_id: str
    model_spec: dict[str, Any]
    agent_spec: dict[str, Any]
    harness_spec: dict[str, Any]
    harness_adapter_contract: dict[str, Any]
    security_profile: dict[str, Any]
    normalized_prompt: str
    acceptance_criteria: list[str]
    allowed_python_modules: list[str]
    allowed_shell_commands: list[str]
    token_budget: int
    max_runtime_seconds: int
    max_changed_files: int
    deployment_policy: DeploymentPolicy
    routing_policy: RoutingPolicy
    routing_decision: dict[str, Any]
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
