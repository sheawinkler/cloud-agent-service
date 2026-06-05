from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cloud_agent_service.models import (
    AgentPlan,
    DeploymentPolicy,
    GitHubIntegrationStatus,
    JobRequest,
    JobResult,
    JobStatus,
    NormalizedPrompt,
    RepoProfile,
    RiskLevel,
    WorkerJobPayload,
)
from cloud_agent_service.store import JobStore

ALLOWED_PYTHON_MODULES = [
    "standard-library",
    "requests",
    "httpx",
    "pydantic",
    "pytest",
    "ruff",
    "mypy",
    "GitPython",
    "openai",
]

ALLOWED_SHELL_COMMANDS = [
    "git",
    "python",
    "python3",
    "pip",
    "pytest",
    "ruff",
    "mypy",
    "npm",
    "pnpm",
    "yarn",
]


class RequestValidationError(ValueError):
    pass


class BudgetExceededError(RuntimeError):
    pass


class RequestValidator:
    def validate(self, request: JobRequest) -> None:
        prompt = request.prompt.strip()
        if not prompt:
            raise RequestValidationError("prompt is required")
        if len(prompt) > request.max_prompt_chars:
            raise RequestValidationError(f"prompt exceeds {request.max_prompt_chars} characters")
        repo_path = Path(request.repo_path).expanduser()
        if not repo_path.exists() or not repo_path.is_dir():
            raise RequestValidationError(f"repo_path is not a directory: {request.repo_path}")
        if request.token_budget <= 0:
            raise RequestValidationError("token_budget must be positive")
        if request.max_runtime_seconds <= 0:
            raise RequestValidationError("max_runtime_seconds must be positive")


class PromptUpgrader:
    def upgrade(self, request: JobRequest) -> NormalizedPrompt:
        prompt = " ".join(request.prompt.strip().split())
        lower = prompt.lower()
        risk = RiskLevel.CODE_EDIT
        if any(term in lower for term in ("deploy", "release", "production")):
            risk = RiskLevel.DEPLOYMENT
        elif any(term in lower for term in ("docker", "ecs", "terraform", "iam", "vpc")):
            risk = RiskLevel.INFRASTRUCTURE_CHANGE
        elif any(term in lower for term in ("dependency", "package", "install")):
            risk = RiskLevel.DEPENDENCY_CHANGE
        elif any(term in lower for term in ("docs", "readme", "outline")):
            risk = RiskLevel.DOCS_ONLY

        acceptance = [
            "Inspect the repository before editing.",
            "Make the smallest change that satisfies the request.",
            "Run deterministic checks before sync or deploy.",
            "Report changed files, commands, and residual risks.",
        ]
        if "buy button" in lower or ("buy" in lower and "button" in lower):
            acceptance.append("A visible Buy button exists on the relevant web page.")

        ambiguities: list[str] = []
        if len(prompt.split()) < 4:
            ambiguities.append(
                "Request is very short; implementation target may be underspecified."
            )

        return NormalizedPrompt(
            brief=prompt,
            acceptance_criteria=acceptance,
            non_goals=["Do not deploy when validation gates fail."],
            risk_level=risk,
            suggested_tests=["python -m compileall ."],
            ambiguities=ambiguities,
        )


class Planner:
    def create_plan(self, request: JobRequest, normalized: NormalizedPrompt) -> AgentPlan:
        return AgentPlan(
            normalized_prompt=normalized.brief,
            acceptance_criteria=normalized.acceptance_criteria,
            allowed_python_modules=ALLOWED_PYTHON_MODULES,
            allowed_shell_commands=ALLOWED_SHELL_COMMANDS,
            disallowed_actions=[
                "Access another user's workspace.",
                "Persist secrets in files or logs.",
                "Deploy when required gates fail.",
            ],
            expected_files_or_areas=self._expected_files(normalized.brief),
            required_tests=normalized.suggested_tests,
            max_tokens=request.token_budget,
            max_runtime_seconds=request.max_runtime_seconds,
            output_schema={
                "changed_files": "list[str]",
                "commands_run": "list[str]",
                "tests_passed": "list[str]",
                "tests_failed": "list[str]",
                "dependency_changes": "list[str]",
                "policy_gate_results": "dict[str, bool]",
                "pr_url": "str|null",
                "deployment_status": "str",
                "residual_risks": "list[str]",
            },
        )

    @staticmethod
    def _expected_files(prompt: str) -> list[str]:
        lower = prompt.lower()
        if "buy button" in lower or ("buy" in lower and "button" in lower):
            return ["*.html", "templates/*", "src/*", "app/*"]
        if "website" in lower:
            return ["*.html", "src/*", "app/*"]
        return ["repo-local files selected after inspection"]


class LocalRepoConnector:
    def clone(self, source_repo: str | Path, workspace_root: str | Path, job_id: str) -> Path:
        source = Path(source_repo).expanduser().resolve()
        destination = Path(workspace_root).expanduser().resolve() / job_id / "repo"
        if destination.exists():
            shutil.rmtree(destination)
        ignore = shutil.ignore_patterns(
            ".git",
            "__pycache__",
            ".venv",
            ".quality-venv",
            ".ruff_cache",
            "node_modules",
            ".agent_cloud",
            ".runtime",
            "runtime",
        )
        shutil.copytree(source, destination, ignore=ignore)
        return destination


class DependencyInstaller:
    def requested_modules(self, plan: AgentPlan) -> list[str]:
        return [
            module
            for module in plan.allowed_python_modules
            if module not in {"standard-library"} and module in {"pytest", "ruff"}
        ]

    def install_command(self, modules: list[str]) -> str:
        if not modules:
            return "no dependency install required"
        return "scripts/install_allowed_modules.sh " + " ".join(modules)


class RepoAnalyzer:
    def analyze(self, repo_path: str | Path) -> RepoProfile:
        repo = Path(repo_path)
        detected_files = self._detected_files(repo)
        package_manager = self._package_manager(repo)
        framework = self._framework(repo, detected_files)
        suggested_tests = self._suggested_tests(repo, package_manager)
        risk_notes: list[str] = []
        if (repo / ".env").exists():
            risk_notes.append("Repository contains a .env file; keep it out of job artifacts.")
        if (repo / ".github" / "workflows").exists():
            risk_notes.append(
                "Repository has GitHub workflow files; workflow edits require review."
            )

        return RepoProfile(
            package_manager=package_manager,
            framework=framework,
            detected_files=detected_files,
            suggested_test_commands=suggested_tests,
            risk_notes=risk_notes,
        )

    @staticmethod
    def _detected_files(repo: Path) -> list[str]:
        candidates = [
            "package.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "package-lock.json",
            "pyproject.toml",
            "requirements.txt",
            "pytest.ini",
            "vite.config.ts",
            "vite.config.js",
            "next.config.js",
            "next.config.mjs",
            "Dockerfile",
            "compose.yaml",
            "docker-compose.yml",
        ]
        return [name for name in candidates if (repo / name).exists()]

    @staticmethod
    def _package_manager(repo: Path) -> str | None:
        if (repo / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (repo / "yarn.lock").exists():
            return "yarn"
        if (repo / "package-lock.json").exists():
            return "npm"
        if (repo / "package.json").exists():
            return "npm"
        if (repo / "pyproject.toml").exists():
            return "python"
        if (repo / "requirements.txt").exists():
            return "pip"
        return None

    @staticmethod
    def _framework(repo: Path, detected_files: list[str]) -> str | None:
        if "next.config.js" in detected_files or "next.config.mjs" in detected_files:
            return "nextjs"
        if "vite.config.ts" in detected_files or "vite.config.js" in detected_files:
            return "vite"
        if (repo / "app.py").exists() or (repo / "cloud_agent_service" / "app.py").exists():
            return "fastapi"
        if list(repo.rglob("*.html")):
            return "static-html"
        return None

    @staticmethod
    def _suggested_tests(repo: Path, package_manager: str | None) -> list[str]:
        commands = ["python3 -m compileall ."]
        package_json = repo / "package.json"
        if package_json.exists():
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            if isinstance(scripts, dict) and "test" in scripts:
                runner = package_manager or "npm"
                commands.append(f"{runner} test")
            if isinstance(scripts, dict) and "build" in scripts:
                runner = package_manager or "npm"
                commands.append(f"{runner} run build")
        return commands


class LocalCodingAgent:
    def execute(self, repo_path: str | Path, plan: AgentPlan) -> dict[str, Any]:
        repo = Path(repo_path)
        changed_files: list[str] = []
        lower = plan.normalized_prompt.lower()

        if "buy button" in lower or ("buy" in lower and "button" in lower):
            changed_files.extend(self._add_buy_button(repo))
        else:
            changed_files.append(self._write_agent_note(repo, plan))

        return {
            "changed_files": sorted(set(changed_files)),
            "commands_run": [],
            "tests_passed": [],
            "tests_failed": [],
            "dependency_changes": [],
            "residual_risks": [],
        }

    def _add_buy_button(self, repo: Path) -> list[str]:
        html_files = sorted(repo.rglob("*.html"))
        target = html_files[0] if html_files else repo / "index.html"
        if target.exists():
            content = target.read_text(encoding="utf-8")
        else:
            content = "<!doctype html>\n<html>\n<body>\n</body>\n</html>\n"

        button = '<button type="button" data-agent="buy-button">Buy</button>'
        if button not in content:
            if "</body>" in content:
                content = content.replace("</body>", f"  {button}\n</body>", 1)
            else:
                content = content.rstrip() + f"\n{button}\n"
            target.write_text(content, encoding="utf-8")
        return [str(target.relative_to(repo))]

    def _write_agent_note(self, repo: Path, plan: AgentPlan) -> str:
        output_dir = repo / "agent_output"
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "implementation_plan.md"
        target.write_text(
            "# Agent Implementation Note\n\n"
            f"Request: {plan.normalized_prompt}\n\n"
            "This local MVP records the requested change for review when no "
            "deterministic edit template matches the prompt.\n",
            encoding="utf-8",
        )
        return str(target.relative_to(repo))


class TestRunner:
    def run(self, repo_path: str | Path) -> tuple[list[str], list[str], list[str]]:
        commands_run: list[str] = []
        tests_passed: list[str] = []
        tests_failed: list[str] = []
        repo = Path(repo_path)

        command = ["python3", "-m", "compileall", "."]
        commands_run.append(" ".join(command))
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            tests_passed.append("python3 -m compileall .")
        else:
            tests_failed.append("python3 -m compileall .")

        return commands_run, tests_passed, tests_failed


class PolicyGate:
    SECRET_PATTERNS = [
        re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ]
    PROTECTED_PATH_PATTERNS = [
        re.compile(r"(^|/)\.env(\.|$)"),
        re.compile(r"(^|/)\.github/workflows/"),
        re.compile(r"(^|/)Dockerfile$"),
        re.compile(r"(^|/)compose\.ya?ml$"),
        re.compile(r"(^|/)docker-compose\.ya?ml$"),
        re.compile(r"(^|/).*\.tf$"),
        re.compile(r"(^|/)secrets?/"),
    ]
    DEPENDENCY_PATHS = {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
    }

    def evaluate(
        self,
        repo_path: str | Path,
        changed_files: list[str],
        tests_failed: list[str],
        max_changed_files: int,
    ) -> dict[str, bool]:
        repo = Path(repo_path)
        diff_size_ok = len(changed_files) <= max_changed_files
        tests_ok = not tests_failed
        secrets_ok = True
        for rel_path in changed_files:
            path = repo / rel_path
            if path.exists() and path.is_file():
                content = path.read_text(encoding="utf-8", errors="ignore")
                if any(pattern.search(content) for pattern in self.SECRET_PATTERNS):
                    secrets_ok = False
                    break
        protected_path_ok = not any(
            pattern.search(rel_path)
            for rel_path in changed_files
            for pattern in self.PROTECTED_PATH_PATTERNS
        )
        dependency_policy_ok = not any(
            Path(rel_path).name in self.DEPENDENCY_PATHS for rel_path in changed_files
        )
        deployment_ok = (
            tests_ok
            and secrets_ok
            and diff_size_ok
            and protected_path_ok
            and dependency_policy_ok
        )

        return {
            "repo_tests": tests_ok,
            "secret_scan": secrets_ok,
            "diff_policy": diff_size_ok,
            "protected_path_policy": protected_path_ok,
            "dependency_policy": dependency_policy_ok,
            "deployment_policy": deployment_ok,
        }


class LocalGitHubSync:
    def sync(self, artifacts_dir: str | Path, job_id: str, result: dict[str, Any]) -> str:
        artifacts = Path(artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)
        pr_path = artifacts / f"{job_id}-pr.json"
        pr_payload = {
            "provider": "local-github-mock",
            "job_id": job_id,
            "changed_files": result["changed_files"],
            "tests_passed": result["tests_passed"],
            "tests_failed": result["tests_failed"],
        }
        pr_path.write_text(json.dumps(pr_payload, indent=2, sort_keys=True), encoding="utf-8")
        return f"local://github/pr/{job_id}"


class GitHubIntegration:
    REQUIRED_ENV = [
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
    ]

    def status(self) -> GitHubIntegrationStatus:
        missing = [name for name in self.REQUIRED_ENV if not os.environ.get(name)]
        return GitHubIntegrationStatus(
            configured=not missing,
            provider="github-app",
            missing=missing,
            mode="ready" if not missing else "local-mock",
        )


class LocalDeployer:
    def deploy(self, artifacts_dir: str | Path, job_id: str, policy: DeploymentPolicy) -> str:
        if policy == DeploymentPolicy.NEVER:
            return "skipped: deployment disabled"
        if policy == DeploymentPolicy.MANUAL:
            return "ready: manual approval required"
        artifacts = Path(artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)
        deploy_path = artifacts / f"{job_id}-deployment.json"
        deploy_path.write_text(
            json.dumps(
                {
                    "provider": "local-deploy-mock",
                    "job_id": job_id,
                    "status": "deployed",
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return "deployed: local mock deployment recorded"


class AgentCloudFlow:
    def __init__(
        self,
        store: JobStore,
        workspace_root: str | Path,
        artifacts_dir: str | Path,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root)
        self.artifacts_dir = Path(artifacts_dir)
        self.validator = RequestValidator()
        self.upgrader = PromptUpgrader()
        self.planner = Planner()
        self.repo_connector = LocalRepoConnector()
        self.repo_analyzer = RepoAnalyzer()
        self.dependency_installer = DependencyInstaller()
        self.agent = LocalCodingAgent()
        self.test_runner = TestRunner()
        self.policy_gate = PolicyGate()
        self.github_sync = LocalGitHubSync()
        self.github_integration = GitHubIntegration()
        self.deployer = LocalDeployer()

    def create_job(self, request: JobRequest) -> str:
        self.validator.validate(request)
        job_id = self._job_id(request)
        self.store.create_job(
            {
                "job_id": job_id,
                "user_id": request.user_id,
                "prompt": request.prompt,
                "repo_path": str(Path(request.repo_path).expanduser().resolve()),
                "base_branch": request.base_branch,
                "deploy_policy": request.deploy_policy.value,
                "token_budget": request.token_budget,
                "max_prompt_chars": request.max_prompt_chars,
                "max_runtime_seconds": request.max_runtime_seconds,
                "max_changed_files": request.max_changed_files,
            }
        )
        self.store.add_event(job_id, "job_created", {"user_id": request.user_id})
        self.store.update_job(job_id, status=JobStatus.QUEUED)
        self.store.add_event(job_id, "job_queued", {})
        return job_id

    def build_worker_payload(
        self,
        job_id: str,
        status_callback_url: str = "local://jobs",
    ) -> WorkerJobPayload:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(f"unknown job_id: {job_id}")

        request = self._request_from_job(job)
        normalized = self.upgrader.upgrade(request)
        plan = self.planner.create_plan(request, normalized)
        return WorkerJobPayload(
            job_id=job_id,
            user_id=request.user_id,
            repo_provider="local",
            repo_path=request.repo_path,
            base_branch=request.base_branch,
            working_branch=f"agent/{job_id}",
            normalized_prompt=plan.normalized_prompt,
            acceptance_criteria=plan.acceptance_criteria,
            allowed_python_modules=plan.allowed_python_modules,
            allowed_shell_commands=plan.allowed_shell_commands,
            token_budget=plan.max_tokens,
            max_runtime_seconds=plan.max_runtime_seconds,
            max_changed_files=request.max_changed_files,
            deployment_policy=request.deploy_policy,
            status_callback_url=f"{status_callback_url.rstrip('/')}/{job_id}",
            output_schema=plan.output_schema,
        )

    def run_next_queued_job(self) -> JobResult | None:
        job_id = self.store.claim_next_queued_job()
        if not job_id:
            return None
        return self.run_job(job_id)

    def cancel_job(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(f"unknown job_id: {job_id}")
        if job["status"] not in {JobStatus.CREATED.value, JobStatus.QUEUED.value}:
            return False
        self.store.update_job(job_id, status=JobStatus.CANCELLED)
        self.store.add_event(job_id, "job_cancelled", {})
        return True

    def retry_job(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(f"unknown job_id: {job_id}")
        if job["status"] not in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            return False
        self.store.update_job(job_id, status=JobStatus.QUEUED)
        self.store.add_event(job_id, "job_retried", {})
        return True

    def approve_deployment(self, job_id: str) -> JobResult:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(f"unknown job_id: {job_id}")
        result = self._stored_result(job)
        if result.status != JobStatus.SUCCEEDED:
            raise RequestValidationError("only succeeded jobs can be approved for deployment")
        if result.deployment_status != "ready: manual approval required":
            return result

        self.store.add_event(job_id, "deployment_approved", {"approved_by": job["user_id"]})
        deployment_status = self.deployer.deploy(
            self.artifacts_dir,
            job_id,
            DeploymentPolicy.LOCAL,
        )
        result.deployment_status = deployment_status
        self.store.add_event(job_id, "deployment_finished", {"status": deployment_status})
        self.store.update_job(job_id, result_json=asdict(result))
        return self._stored_result(self.store.get_job(job_id))

    def github_status(self) -> GitHubIntegrationStatus:
        return self.github_integration.status()

    def run_job(self, job_id: str) -> JobResult:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(f"unknown job_id: {job_id}")
        if job["status"] in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value}:
            return self._stored_result(job)
        if job["status"] == JobStatus.CANCELLED.value:
            raise RequestValidationError(f"job is cancelled: {job_id}")

        request = self._request_from_job(job)
        agent_result = self._empty_agent_result()

        try:
            self._charge_budget(job_id, request, "dispatch", 64, "worker dispatch envelope")
            self.store.update_job(job_id, status=JobStatus.DISPATCHED)
            self.store.add_event(job_id, "agent_dispatched", {"mode": "local-container-contract"})

            workspace_repo = self.repo_connector.clone(
                request.repo_path, self.workspace_root, job_id
            )
            self.store.update_job(
                job_id, workspace_path=str(workspace_repo), status=JobStatus.RUNNING
            )
            self.store.add_event(job_id, "repo_cloned", {"workspace_path": str(workspace_repo)})

            repo_profile = self.repo_analyzer.analyze(workspace_repo)
            self._charge_budget(job_id, request, "repo_analysis", 128, "repo profile detection")
            self.store.add_event(job_id, "repo_analyzed", asdict(repo_profile))

            normalized = self.upgrader.upgrade(request)
            self._charge_budget(
                job_id,
                request,
                "prompt_upgrade",
                self._estimated_tokens(normalized.brief),
                "normalized user request",
            )
            plan = self.planner.create_plan(request, normalized)
            self.store.update_job(job_id, normalized_prompt=normalized.brief)
            self.store.add_event(job_id, "prompt_upgraded", asdict(normalized))
            self.store.add_event(job_id, "plan_created", asdict(plan))

            modules = self.dependency_installer.requested_modules(plan)
            install_command = self.dependency_installer.install_command(modules)
            self.store.add_event(
                job_id,
                "dependencies_requested",
                {"modules": modules, "install_command": install_command},
            )

            agent_result = self.agent.execute(workspace_repo, plan)
            self._charge_budget(job_id, request, "agent_edit", 512, "local deterministic edit")
            self.store.add_event(
                job_id,
                "files_changed",
                {"changed_files": agent_result["changed_files"]},
            )

            self.store.update_job(job_id, status=JobStatus.VALIDATING)
            commands_run, tests_passed, tests_failed = self.test_runner.run(workspace_repo)
            self._charge_budget(job_id, request, "validation", 128, "deterministic checks")
            agent_result["commands_run"].extend(commands_run)
            agent_result["tests_passed"].extend(tests_passed)
            agent_result["tests_failed"].extend(tests_failed)
            self.store.add_event(
                job_id,
                "tests_finished",
                {"passed": tests_passed, "failed": tests_failed, "commands": commands_run},
            )

            gates = self.policy_gate.evaluate(
                workspace_repo,
                agent_result["changed_files"],
                tests_failed,
                request.max_changed_files,
            )
            self.store.add_event(job_id, "policy_gate_result", gates)
            if not all(gates.values()):
                return self._fail(job_id, agent_result, gates, "not deployed: policy gate failed")

            self.store.update_job(job_id, status=JobStatus.SYNCING)
            pr_url = self.github_sync.sync(self.artifacts_dir, job_id, agent_result)
            self._charge_budget(job_id, request, "sync", 64, "mock PR sync")
            self.store.add_event(job_id, "branch_pushed", {"provider": "local-github-mock"})
            self.store.add_event(job_id, "pr_created_or_updated", {"pr_url": pr_url})

            self.store.update_job(job_id, status=JobStatus.DEPLOYING)
            deployment_status = self.deployer.deploy(
                self.artifacts_dir, job_id, request.deploy_policy
            )
            self.store.add_event(job_id, "deployment_finished", {"status": deployment_status})

            self.store.update_job(job_id, status=JobStatus.SUCCEEDED)
            self.store.add_event(job_id, "job_succeeded", {})
            result = self._result(
                job_id=job_id,
                status=JobStatus.SUCCEEDED,
                agent_result=agent_result,
                gates=gates,
                pr_url=pr_url,
                deployment_status=deployment_status,
                residual_risks=[],
            )
            self.store.update_job(
                job_id,
                status=JobStatus.SUCCEEDED,
                result_json=asdict(result),
            )
            return result
        except BudgetExceededError as exc:
            gates = {
                "repo_tests": False,
                "secret_scan": True,
                "diff_policy": True,
                "protected_path_policy": True,
                "dependency_policy": True,
                "deployment_policy": False,
            }
            self.store.add_event(job_id, "budget_exceeded", {"error": str(exc)})
            return self._fail(job_id, agent_result, gates, "not deployed: budget exceeded")
        except Exception as exc:
            self.store.add_event(job_id, "job_failed", {"error": str(exc)})
            self.store.update_job(job_id, status=JobStatus.FAILED)
            raise

    @staticmethod
    def _request_from_job(job: dict[str, Any]) -> JobRequest:
        return JobRequest(
            prompt=job["prompt"],
            repo_path=job["repo_path"],
            user_id=job["user_id"],
            base_branch=job["base_branch"],
            deploy_policy=DeploymentPolicy(job["deploy_policy"]),
            token_budget=job["token_budget"],
            max_prompt_chars=job["max_prompt_chars"],
            max_runtime_seconds=job["max_runtime_seconds"],
            max_changed_files=job["max_changed_files"],
        )

    @staticmethod
    def _estimated_tokens(text: str) -> int:
        return max(1, len(text) // 4)

    @staticmethod
    def _empty_agent_result() -> dict[str, Any]:
        return {
            "changed_files": [],
            "commands_run": [],
            "tests_passed": [],
            "tests_failed": [],
            "dependency_changes": [],
            "residual_risks": [],
        }

    def _charge_budget(
        self,
        job_id: str,
        request: JobRequest,
        stage: str,
        token_delta: int,
        note: str,
    ) -> None:
        self.store.add_budget_entry(job_id, stage, token_delta, note=note)
        tokens_used = self.store.budget_tokens_used(job_id)
        self.store.add_event(
            job_id,
            "budget_charged",
            {
                "stage": stage,
                "token_delta": token_delta,
                "tokens_used": tokens_used,
                "token_budget": request.token_budget,
            },
        )
        if tokens_used > request.token_budget:
            raise BudgetExceededError(
                f"token budget exceeded: used {tokens_used} > budget {request.token_budget}"
            )

    def _stored_result(self, job: dict[str, Any]) -> JobResult:
        result = job["result_json"]
        if not result:
            raise RequestValidationError(f"job has terminal status without result: {job['job_id']}")
        return JobResult(
            job_id=result["job_id"],
            status=JobStatus(result["status"]),
            changed_files=result["changed_files"],
            commands_run=result["commands_run"],
            tests_passed=result["tests_passed"],
            tests_failed=result["tests_failed"],
            dependency_changes=result["dependency_changes"],
            policy_gate_results=result["policy_gate_results"],
            pr_url=result["pr_url"],
            deployment_status=result["deployment_status"],
            residual_risks=result["residual_risks"],
            events=self.store.list_events(result["job_id"]),
        )

    def _fail(
        self,
        job_id: str,
        agent_result: dict[str, Any],
        gates: dict[str, bool],
        deployment_status: str,
    ) -> JobResult:
        result = self._result(
            job_id=job_id,
            status=JobStatus.FAILED,
            agent_result=agent_result,
            gates=gates,
            pr_url=None,
            deployment_status=deployment_status,
            residual_risks=["Validation failed; branch was not synced or deployed."],
        )
        self.store.update_job(job_id, status=JobStatus.FAILED, result_json=asdict(result))
        self.store.add_event(job_id, "job_failed", {"reason": deployment_status})
        return result

    def _result(
        self,
        job_id: str,
        status: JobStatus,
        agent_result: dict[str, Any],
        gates: dict[str, bool],
        pr_url: str | None,
        deployment_status: str,
        residual_risks: list[str],
    ) -> JobResult:
        return JobResult(
            job_id=job_id,
            status=status,
            changed_files=agent_result["changed_files"],
            commands_run=agent_result["commands_run"],
            tests_passed=agent_result["tests_passed"],
            tests_failed=agent_result["tests_failed"],
            dependency_changes=agent_result["dependency_changes"],
            policy_gate_results=gates,
            pr_url=pr_url,
            deployment_status=deployment_status,
            residual_risks=residual_risks + agent_result.get("residual_risks", []),
            events=self.store.list_events(job_id),
        )

    @staticmethod
    def _job_id(request: JobRequest) -> str:
        payload = "|".join(
            [
                request.user_id,
                str(Path(request.repo_path).expanduser().resolve()),
                request.base_branch,
                request.prompt,
                os.urandom(8).hex(),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
