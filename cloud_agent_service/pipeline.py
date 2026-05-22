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
    JobRequest,
    JobResult,
    JobStatus,
    NormalizedPrompt,
    RiskLevel,
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

        return {
            "repo_tests": tests_ok,
            "secret_scan": secrets_ok,
            "diff_policy": diff_size_ok,
            "dependency_policy": True,
            "deployment_policy": tests_ok and secrets_ok and diff_size_ok,
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
        self.dependency_installer = DependencyInstaller()
        self.agent = LocalCodingAgent()
        self.test_runner = TestRunner()
        self.policy_gate = PolicyGate()
        self.github_sync = LocalGitHubSync()
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
            }
        )
        self.store.add_event(job_id, "job_created", {"user_id": request.user_id})
        self.store.update_job(job_id, status=JobStatus.QUEUED)
        self.store.add_event(job_id, "job_queued", {})
        return job_id

    def run_job(self, job_id: str) -> JobResult:
        job = self.store.get_job(job_id)
        if not job:
            raise KeyError(f"unknown job_id: {job_id}")

        request = JobRequest(
            prompt=job["prompt"],
            repo_path=job["repo_path"],
            user_id=job["user_id"],
            base_branch=job["base_branch"],
            deploy_policy=DeploymentPolicy(job["deploy_policy"]),
            token_budget=job["token_budget"],
        )

        try:
            self.store.update_job(job_id, status=JobStatus.DISPATCHED)
            self.store.add_event(job_id, "agent_dispatched", {"mode": "local-container-contract"})

            workspace_repo = self.repo_connector.clone(
                request.repo_path, self.workspace_root, job_id
            )
            self.store.update_job(
                job_id, workspace_path=str(workspace_repo), status=JobStatus.RUNNING
            )
            self.store.add_event(job_id, "repo_cloned", {"workspace_path": str(workspace_repo)})

            normalized = self.upgrader.upgrade(request)
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
            self.store.add_event(
                job_id,
                "files_changed",
                {"changed_files": agent_result["changed_files"]},
            )

            self.store.update_job(job_id, status=JobStatus.VALIDATING)
            commands_run, tests_passed, tests_failed = self.test_runner.run(workspace_repo)
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
        except Exception as exc:
            self.store.add_event(job_id, "job_failed", {"error": str(exc)})
            self.store.update_job(job_id, status=JobStatus.FAILED)
            raise

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
