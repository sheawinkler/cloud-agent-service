from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cloud_agent_service.models import (
    AgentPlan,
    AgentSpec,
    DeploymentPolicy,
    GitHubIntegrationStatus,
    JobRequest,
    JobResult,
    JobStatus,
    ModelSpec,
    NormalizedPrompt,
    PreviewArtifact,
    PromotionDecision,
    PromotionStatus,
    RepoProfile,
    RepoProvider,
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


class GitHubAppError(RuntimeError):
    pass


class GitProviderError(RuntimeError):
    pass


class ModelAgentRegistry:
    def __init__(self) -> None:
        self.models = {
            "local-deterministic": ModelSpec(
                model_id="local-deterministic",
                provider="local",
                name="deterministic-repo-editor",
                context_window=8_000,
                cost_tier="free-local",
                supports_tools=True,
            ),
            "gpt-5-coding": ModelSpec(
                model_id="gpt-5-coding",
                provider="openai",
                name="gpt-5-coding",
                context_window=128_000,
                cost_tier="external-api",
                supports_tools=True,
            ),
        }
        self.agents = {
            "repo-editor-v1": AgentSpec(
                agent_id="repo-editor-v1",
                role="repo_editor",
                model_id="local-deterministic",
                allowed_shell_commands=ALLOWED_SHELL_COMMANDS,
                output_contract="job_result.v1",
            ),
            "repo-reviewer-v1": AgentSpec(
                agent_id="repo-reviewer-v1",
                role="repo_reviewer",
                model_id="gpt-5-coding",
                allowed_shell_commands=["git", "python3", "pytest", "ruff"],
                output_contract="promotion_decision.v1",
            ),
        }

    def validate(self, request: JobRequest) -> None:
        if request.model_id not in self.models:
            raise RequestValidationError(f"unknown model_id: {request.model_id}")
        if request.agent_id not in self.agents:
            raise RequestValidationError(f"unknown agent_id: {request.agent_id}")
        agent = self.agents[request.agent_id]
        if agent.model_id != request.model_id:
            raise RequestValidationError(
                f"agent_id {request.agent_id} requires model_id {agent.model_id}"
            )

    def model(self, model_id: str) -> ModelSpec:
        return self.models[model_id]

    def agent(self, agent_id: str) -> AgentSpec:
        return self.agents[agent_id]


class RequestValidator:
    def validate(self, request: JobRequest) -> None:
        prompt = request.prompt.strip()
        if not prompt:
            raise RequestValidationError("prompt is required")
        if len(prompt) > request.max_prompt_chars:
            raise RequestValidationError(f"prompt exceeds {request.max_prompt_chars} characters")
        if request.repo_provider == RepoProvider.LOCAL:
            repo_path = Path(request.repo_path).expanduser()
            if not request.repo_path or not repo_path.exists() or not repo_path.is_dir():
                raise RequestValidationError(f"repo_path is not a directory: {request.repo_path}")
        elif request.repo_provider == RepoProvider.GITHUB:
            if not request.github_repo or not re.fullmatch(r"[^/\s]+/[^/\s]+", request.github_repo):
                raise RequestValidationError("github_repo must be in owner/repo format")
        elif request.repo_provider == RepoProvider.GIT:
            if not request.git_url:
                raise RequestValidationError("git_url is required for generic Git jobs")
            self._validate_git_url(request.git_url)
        if request.token_budget <= 0:
            raise RequestValidationError("token_budget must be positive")
        if request.max_runtime_seconds <= 0:
            raise RequestValidationError("max_runtime_seconds must be positive")

    @staticmethod
    def _validate_git_url(git_url: str) -> None:
        if git_url.startswith("-") or any(char.isspace() for char in git_url):
            raise RequestValidationError("git_url must be a single Git remote value")
        parsed = urllib.parse.urlparse(git_url)
        if parsed.username or parsed.password:
            raise RequestValidationError(
                "git_url must not embed credentials; use runtime Git credentials instead"
            )


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
                "promotion_decision": "dict[str, object]",
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


class GitHubAppClient:
    API_VERSION = "2022-11-28"

    def __init__(
        self,
        app_id: str | None = None,
        installation_id: str | None = None,
        private_key: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self.app_id = app_id or os.environ.get("GITHUB_APP_ID", "")
        self.installation_id = installation_id or os.environ.get(
            "GITHUB_APP_INSTALLATION_ID",
            "",
        )
        self.private_key = private_key or os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL", "https://api.github.com")).rstrip(
            "/"
        )

    def status(self) -> GitHubIntegrationStatus:
        missing = []
        if not self.app_id:
            missing.append("GITHUB_APP_ID")
        if not self.installation_id:
            missing.append("GITHUB_APP_INSTALLATION_ID")
        if not self.private_key:
            missing.append("GITHUB_APP_PRIVATE_KEY")
        return GitHubIntegrationStatus(
            configured=not missing,
            provider="github-app",
            missing=missing,
            mode="ready" if not missing else "local-mock",
        )

    def installation_token(self) -> str:
        status = self.status()
        if not status.configured:
            raise GitHubAppError("GitHub App is not configured")
        payload = self._request_json(
            "POST",
            f"/app/installations/{self.installation_id}/access_tokens",
            auth_token=self._jwt(),
            jwt_auth=True,
        )
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError("GitHub installation token response did not include token")
        return token

    def create_pull_request(
        self,
        token: str,
        repo_full_name: str,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str:
        existing = self._find_existing_pr(token, repo_full_name, branch)
        if existing:
            return existing
        payload = self._request_json(
            "POST",
            f"/repos/{repo_full_name}/pulls",
            auth_token=token,
            body={
                "title": title,
                "head": branch,
                "base": base_branch,
                "body": body,
            },
        )
        html_url = payload.get("html_url")
        if not isinstance(html_url, str):
            raise GitHubAppError("GitHub pull request response did not include html_url")
        number = payload.get("number")
        if isinstance(number, int):
            self.create_issue_comment(token, repo_full_name, number, body)
        return html_url

    def create_issue_comment(
        self,
        token: str,
        repo_full_name: str,
        issue_number: int,
        body: str,
    ) -> None:
        self._request_json(
            "POST",
            f"/repos/{repo_full_name}/issues/{issue_number}/comments",
            auth_token=token,
            body={"body": body},
        )

    def _find_existing_pr(self, token: str, repo_full_name: str, branch: str) -> str | None:
        owner = repo_full_name.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "head": f"{owner}:{branch}",
                "per_page": "1",
            }
        )
        payload = self._request_json(
            "GET",
            f"/repos/{repo_full_name}/pulls?{query}",
            auth_token=token,
        )
        if isinstance(payload, list) and payload:
            html_url = payload[0].get("html_url")
            return html_url if isinstance(html_url, str) else None
        return None

    def _jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise GitHubAppError("PyJWT is required for GitHub App JWT signing") from exc
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": self.app_id,
        }
        private_key = self.private_key.replace("\\n", "\n")
        return jwt.encode(payload, private_key, algorithm="RS256")

    def _request_json(
        self,
        method: str,
        path: str,
        auth_token: str,
        body: dict[str, Any] | None = None,
        jwt_auth: bool = False,
    ) -> Any:
        data = None
        headers = {
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {auth_token}",
            "x-github-api-version": self.API_VERSION,
            "user-agent": "cloud-agent-service",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(
            self.api_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8")
            auth_kind = "JWT" if jwt_auth else "installation token"
            raise GitHubAppError(
                f"GitHub API {method} {path} failed with {exc.code} using {auth_kind}: {detail}"
            ) from exc
        return json.loads(text) if text else {}


class GitHubRepoConnector:
    def __init__(self, client: GitHubAppClient) -> None:
        self.client = client

    def clone(
        self,
        repo_full_name: str,
        base_branch: str,
        workspace_root: str | Path,
        job_id: str,
    ) -> tuple[Path, str]:
        token = self.client.installation_token()
        destination = Path(workspace_root).expanduser().resolve() / job_id / "repo"
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        remote = f"https://github.com/{repo_full_name}.git"
        command = [
            "git",
            "-c",
            f"http.extraheader=Authorization: Bearer {token}",
            "clone",
            "--branch",
            base_branch,
            "--single-branch",
            remote,
            str(destination),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise GitHubAppError("GitHub clone failed")
        return destination, token


class GitRepoConnector:
    def clone(
        self,
        git_url: str,
        base_branch: str,
        workspace_root: str | Path,
        job_id: str,
    ) -> Path:
        destination = Path(workspace_root).expanduser().resolve() / job_id / "repo"
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = self._git_command_prefix() + [
            "clone",
            "--branch",
            base_branch,
            "--single-branch",
            git_url,
            str(destination),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise GitProviderError("generic Git clone failed")
        return destination

    @staticmethod
    def _git_command_prefix() -> list[str]:
        extra_header = os.environ.get("GIT_HTTP_EXTRAHEADER")
        if extra_header:
            return ["git", "-c", f"http.extraheader={extra_header}"]
        return ["git"]


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


class GenericGitSync:
    def sync(
        self,
        repo_path: str | Path,
        working_branch: str,
        result: dict[str, Any],
    ) -> str:
        repo = Path(repo_path)
        self._git(repo, ["config", "user.name", "cloud-agent-service"])
        self._git(repo, ["config", "user.email", "cloud-agent-service@users.noreply.github.com"])
        self._git(repo, ["checkout", "-B", working_branch])
        for rel_path in result["changed_files"]:
            self._git(repo, ["add", rel_path])
        status = self._git(repo, ["status", "--porcelain"])
        if status.strip():
            self._git(repo, ["commit", "-m", "Apply cloud agent changes"])
        self._git(
            repo,
            ["push", "origin", f"HEAD:refs/heads/{working_branch}", "--force-with-lease"],
            include_auth=True,
        )
        return f"git://review/{working_branch}"

    @staticmethod
    def _git(repo: Path, args: list[str], include_auth: bool = False) -> str:
        command = ["git", *args]
        if include_auth and os.environ.get("GIT_HTTP_EXTRAHEADER"):
            command = ["git", "-c", f"http.extraheader={os.environ['GIT_HTTP_EXTRAHEADER']}", *args]
        result = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitProviderError(f"git {' '.join(args[:2])} failed")
        return result.stdout


class GitHubAppSync:
    def __init__(self, client: GitHubAppClient) -> None:
        self.client = client

    def sync(
        self,
        repo_path: str | Path,
        repo_full_name: str,
        base_branch: str,
        working_branch: str,
        token: str,
        result: dict[str, Any],
        evidence: dict[str, Any],
    ) -> str:
        repo = Path(repo_path)
        self._git(repo, ["config", "user.name", "cloud-agent-service[bot]"])
        self._git(repo, ["config", "user.email", "cloud-agent-service@users.noreply.github.com"])
        self._git(repo, ["checkout", "-B", working_branch])
        for rel_path in result["changed_files"]:
            self._git(repo, ["add", rel_path])
        status = self._git(repo, ["status", "--porcelain"])
        if status.strip():
            self._git(repo, ["commit", "-m", "Apply cloud agent changes"])
            self._git(
                repo,
                [
                    "-c",
                    f"http.extraheader=Authorization: Bearer {token}",
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{working_branch}",
                    "--force-with-lease",
                ],
            )
        body = self._pr_body(result, evidence)
        return self.client.create_pull_request(
            token=token,
            repo_full_name=repo_full_name,
            branch=working_branch,
            base_branch=base_branch,
            title="Apply cloud agent changes",
            body=body,
        )

    @staticmethod
    def _git(repo: Path, args: list[str]) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise GitHubAppError(f"git {' '.join(args[:2])} failed")
        return result.stdout

    @staticmethod
    def _pr_body(result: dict[str, Any], evidence: dict[str, Any]) -> str:
        changed = "\n".join(f"- `{path}`" for path in result["changed_files"]) or "- none"
        tests = "\n".join(f"- {test}" for test in result["tests_passed"]) or "- none"
        risks = "\n".join(f"- {risk}" for risk in result.get("residual_risks", [])) or "- none"
        preview_url = evidence.get("preview_url") or "not available"
        return (
            "## Cloud Agent Result\n\n"
            f"Preview: `{preview_url}`\n\n"
            "### Changed files\n"
            f"{changed}\n\n"
            "### Tests passed\n"
            f"{tests}\n\n"
            "### Residual risks\n"
            f"{risks}\n"
        )


class GitHubIntegration:
    REQUIRED_ENV = [
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
    ]

    def __init__(self, client: GitHubAppClient | None = None) -> None:
        self.client = client or GitHubAppClient()

    def status(self) -> GitHubIntegrationStatus:
        return self.client.status()


class PreviewPublisher:
    def publish(
        self,
        repo_path: str | Path,
        artifacts_dir: str | Path,
        job_id: str,
        changed_files: list[str],
    ) -> PreviewArtifact:
        repo = Path(repo_path)
        artifacts = Path(artifacts_dir)
        preview_dir = artifacts / "previews" / job_id
        preview_dir.mkdir(parents=True, exist_ok=True)
        html_candidates = [
            rel_path for rel_path in changed_files if rel_path.endswith(".html")
        ] or [str(path.relative_to(repo)) for path in sorted(repo.rglob("*.html"))[:1]]
        if not html_candidates:
            proof_path = preview_dir / "browser-proof.json"
            proof_path.write_text(
                json.dumps({"checks": {"html_preview_available": False}}, indent=2),
                encoding="utf-8",
            )
            return PreviewArtifact(
                preview_url=None,
                artifact_path=None,
                browser_proof_path=str(proof_path),
                checks={"html_preview_available": False},
            )

        source = repo / html_candidates[0]
        target = preview_dir / Path(html_candidates[0]).name
        shutil.copy2(source, target)
        content = target.read_text(encoding="utf-8", errors="ignore")
        checks = {
            "html_preview_available": True,
            "buy_button_present": 'data-agent="buy-button"' in content,
        }
        proof_path = preview_dir / "browser-proof.json"
        proof_path.write_text(
            json.dumps(
                {
                    "provider": "local-html-proof",
                    "source_file": html_candidates[0],
                    "checks": checks,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return PreviewArtifact(
            preview_url=f"local://preview/{job_id}/{target.name}",
            artifact_path=str(target),
            browser_proof_path=str(proof_path),
            checks=checks,
        )


class LocalDeployer:
    def deploy(self, artifacts_dir: str | Path, job_id: str, policy: DeploymentPolicy) -> str:
        if policy == DeploymentPolicy.NEVER:
            return "skipped: deployment disabled"
        if policy in {
            DeploymentPolicy.MANUAL,
            DeploymentPolicy.PRODUCTION_APPROVAL,
        }:
            return "ready: manual approval required"
        if policy == DeploymentPolicy.PR_ONLY:
            return "skipped: PR only"
        if policy == DeploymentPolicy.PREVIEW_ONLY:
            return "ready: preview only"
        artifacts = Path(artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)
        deploy_path = artifacts / f"{job_id}-deployment.json"
        deploy_path.write_text(
            json.dumps(
                {
                    "provider": "local-deploy-mock",
                    "job_id": job_id,
                    "status": "deployed",
                    "policy": policy.value,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if policy == DeploymentPolicy.STAGING_AUTO:
            return "deployed: local staging mock deployment recorded"
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
        self.lab_registry = ModelAgentRegistry()
        self.validator = RequestValidator()
        self.upgrader = PromptUpgrader()
        self.planner = Planner()
        self.repo_connector = LocalRepoConnector()
        self.git_repo_connector = GitRepoConnector()
        self.github_client = GitHubAppClient()
        self.github_repo_connector = GitHubRepoConnector(self.github_client)
        self.repo_analyzer = RepoAnalyzer()
        self.dependency_installer = DependencyInstaller()
        self.agent = LocalCodingAgent()
        self.test_runner = TestRunner()
        self.policy_gate = PolicyGate()
        self.github_sync = LocalGitHubSync()
        self.git_sync = GenericGitSync()
        self.github_app_sync = GitHubAppSync(self.github_client)
        self.github_integration = GitHubIntegration(self.github_client)
        self.preview_publisher = PreviewPublisher()
        self.deployer = LocalDeployer()

    def create_job(self, request: JobRequest) -> str:
        self.validator.validate(request)
        self.lab_registry.validate(request)
        job_id = self._job_id(request)
        self.store.create_job(
            {
                "job_id": job_id,
                "user_id": request.user_id,
                "prompt": request.prompt,
                "repo_path": self._repo_path_for_store(request),
                "repo_provider": request.repo_provider.value,
                "git_url": request.git_url,
                "github_repo": request.github_repo,
                "parent_job_id": request.parent_job_id,
                "model_id": request.model_id,
                "agent_id": request.agent_id,
                "working_branch": self._working_branch(job_id, request),
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
        model_spec = self.lab_registry.model(request.model_id)
        agent_spec = self.lab_registry.agent(request.agent_id)
        normalized = self.upgrader.upgrade(request)
        plan = self.planner.create_plan(request, normalized)
        return WorkerJobPayload(
            job_id=job_id,
            user_id=request.user_id,
            repo_provider=request.repo_provider.value,
            repo_path=request.repo_path,
            git_url=request.git_url,
            github_repo=request.github_repo,
            base_branch=request.base_branch,
            working_branch=job["working_branch"] or f"agent/{job_id}",
            parent_job_id=request.parent_job_id,
            model_id=request.model_id,
            agent_id=request.agent_id,
            model_spec=asdict(model_spec),
            agent_spec=asdict(agent_spec),
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
        result.promotion_decision = asdict(
            self._promotion_decision(
                status=result.status,
                agent_result={
                    "changed_files": result.changed_files,
                    "tests_failed": result.tests_failed,
                },
                gates=result.policy_gate_results,
                deployment_status=deployment_status,
                evidence=result.evidence,
            )
        )
        self.store.add_event(
            job_id,
            "promotion_decision_created",
            result.promotion_decision,
        )
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
        model_spec = self.lab_registry.model(request.model_id)
        agent_spec = self.lab_registry.agent(request.agent_id)
        agent_result = self._empty_agent_result()
        evidence: dict[str, Any] = {}

        try:
            self._charge_budget(job_id, request, "dispatch", 64, "worker dispatch envelope")
            self.store.update_job(job_id, status=JobStatus.DISPATCHED)
            self.store.add_event(job_id, "agent_dispatched", {"mode": "local-container-contract"})
            self.store.add_event(
                job_id,
                "lab_run_configured",
                {
                    "model_id": request.model_id,
                    "agent_id": request.agent_id,
                    "model_provider": model_spec.provider,
                    "agent_role": agent_spec.role,
                },
            )

            github_token: str | None = None
            if request.repo_provider == RepoProvider.GITHUB:
                if not request.github_repo:
                    raise RequestValidationError("github_repo is required for GitHub jobs")
                workspace_repo, github_token = self.github_repo_connector.clone(
                    request.github_repo,
                    request.base_branch,
                    self.workspace_root,
                    job_id,
                )
            elif request.repo_provider == RepoProvider.GIT:
                if not request.git_url:
                    raise RequestValidationError("git_url is required for generic Git jobs")
                workspace_repo = self.git_repo_connector.clone(
                    request.git_url,
                    request.base_branch,
                    self.workspace_root,
                    job_id,
                )
            else:
                workspace_repo = self.repo_connector.clone(
                    request.repo_path,
                    self.workspace_root,
                    job_id,
                )
            self.store.update_job(
                job_id, workspace_path=str(workspace_repo), status=JobStatus.RUNNING
            )
            self.store.add_event(job_id, "repo_cloned", {"workspace_path": str(workspace_repo)})

            repo_profile = self.repo_analyzer.analyze(workspace_repo)
            repo_key = self._repo_key(request)
            self.store.upsert_repo_memory(
                repo_key,
                request.repo_provider.value,
                asdict(repo_profile),
                repo_profile.suggested_test_commands,
                job_id,
            )
            self._charge_budget(job_id, request, "repo_analysis", 128, "repo profile detection")
            self.store.add_event(job_id, "repo_analyzed", asdict(repo_profile))
            repo_memory = self.store.get_repo_memory(repo_key)
            if repo_memory:
                self.store.add_event(
                    job_id,
                    "repo_memory_loaded",
                    {
                        "repo_key": repo_key,
                        "last_job_id": repo_memory["last_job_id"],
                    },
                )

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

            preview = self.preview_publisher.publish(
                workspace_repo,
                self.artifacts_dir,
                job_id,
                agent_result["changed_files"],
            )
            evidence = self._build_evidence(
                job_id,
                preview,
                repo_profile,
                request,
                model_spec,
                agent_spec,
            )
            self.store.add_event(job_id, "preview_created", asdict(preview))
            self.store.add_event(job_id, "browser_proof_finished", preview.checks)

            self.store.update_job(job_id, status=JobStatus.SYNCING)
            if request.repo_provider == RepoProvider.GITHUB:
                if not request.github_repo or not github_token:
                    raise RequestValidationError("GitHub sync requires github_repo and token")
                pr_url = self.github_app_sync.sync(
                    workspace_repo,
                    request.github_repo,
                    request.base_branch,
                    job["working_branch"] or f"agent/{job_id}",
                    github_token,
                    agent_result,
                    evidence,
                )
                sync_provider = "github-app"
            elif request.repo_provider == RepoProvider.GIT:
                pr_url = self.git_sync.sync(
                    workspace_repo,
                    job["working_branch"] or f"agent/{job_id}",
                    agent_result,
                )
                sync_provider = "generic-git"
            else:
                pr_url = self.github_sync.sync(self.artifacts_dir, job_id, agent_result)
                sync_provider = "local-github-mock"
            self._charge_budget(job_id, request, "sync", 64, "PR sync")
            self.store.add_event(job_id, "branch_pushed", {"provider": sync_provider})
            self.store.add_event(job_id, "pr_created_or_updated", {"pr_url": pr_url})

            self.store.update_job(job_id, status=JobStatus.DEPLOYING)
            deployment_status = self.deployer.deploy(
                self.artifacts_dir, job_id, request.deploy_policy
            )
            self.store.add_event(job_id, "deployment_finished", {"status": deployment_status})

            self.store.update_job(job_id, status=JobStatus.SUCCEEDED)
            self.store.add_event(job_id, "job_succeeded", {})
            promotion_decision = self._promotion_decision(
                status=JobStatus.SUCCEEDED,
                agent_result=agent_result,
                gates=gates,
                deployment_status=deployment_status,
                evidence=evidence,
            )
            self.store.add_event(
                job_id,
                "promotion_decision_created",
                asdict(promotion_decision),
            )
            result = self._result(
                job_id=job_id,
                status=JobStatus.SUCCEEDED,
                agent_result=agent_result,
                gates=gates,
                pr_url=pr_url,
                deployment_status=deployment_status,
                residual_risks=[],
                evidence=evidence,
                promotion_decision=promotion_decision,
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
            repo_provider=RepoProvider(job["repo_provider"]),
            git_url=job["git_url"],
            github_repo=job["github_repo"],
            parent_job_id=job["parent_job_id"],
            model_id=job["model_id"],
            agent_id=job["agent_id"],
            user_id=job["user_id"],
            base_branch=job["base_branch"],
            deploy_policy=DeploymentPolicy(job["deploy_policy"]),
            token_budget=job["token_budget"],
            max_prompt_chars=job["max_prompt_chars"],
            max_runtime_seconds=job["max_runtime_seconds"],
            max_changed_files=job["max_changed_files"],
        )

    @staticmethod
    def _repo_path_for_store(request: JobRequest) -> str:
        if request.repo_provider in {RepoProvider.GIT, RepoProvider.GITHUB}:
            return ""
        return str(Path(request.repo_path).expanduser().resolve())

    def _working_branch(self, job_id: str, request: JobRequest) -> str:
        if request.parent_job_id:
            parent = self.store.get_job(request.parent_job_id)
            if parent and parent.get("working_branch"):
                return parent["working_branch"]
        return f"agent/{job_id}"

    @staticmethod
    def _repo_key(request: JobRequest) -> str:
        if request.repo_provider == RepoProvider.GITHUB and request.github_repo:
            return f"github:{request.github_repo}"
        if request.repo_provider == RepoProvider.GIT and request.git_url:
            return f"git:{request.git_url}"
        return f"local:{Path(request.repo_path).expanduser().resolve()}"

    @staticmethod
    def _build_evidence(
        job_id: str,
        preview: PreviewArtifact,
        repo_profile: RepoProfile,
        request: JobRequest,
        model_spec: ModelSpec,
        agent_spec: AgentSpec,
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "repo_provider": request.repo_provider.value,
            "git_target": AgentCloudFlow._safe_git_target(request.git_url),
            "github_repo": request.github_repo,
            "model_spec": asdict(model_spec),
            "agent_spec": asdict(agent_spec),
            "preview_url": preview.preview_url,
            "preview_artifact_path": preview.artifact_path,
            "browser_proof_path": preview.browser_proof_path,
            "browser_checks": preview.checks,
            "repo_profile": asdict(repo_profile),
            "next_action": "review_pr",
        }

    @staticmethod
    def _promotion_decision(
        status: JobStatus,
        agent_result: dict[str, Any],
        gates: dict[str, bool],
        deployment_status: str,
        evidence: dict[str, Any],
    ) -> PromotionDecision:
        base_evidence = {
            "tests_failed": agent_result["tests_failed"],
            "changed_files": agent_result["changed_files"],
            "policy_gate_results": gates,
            "deployment_status": deployment_status,
            "preview_url": evidence.get("preview_url"),
        }
        if status != JobStatus.SUCCEEDED:
            return PromotionDecision(
                status=PromotionStatus.REJECT,
                reason="Run failed before satisfying validation and policy gates.",
                evidence=base_evidence,
            )
        if agent_result["tests_failed"] or not all(gates.values()):
            return PromotionDecision(
                status=PromotionStatus.REJECT,
                reason="Tests or policy gates failed.",
                evidence=base_evidence,
            )
        if deployment_status.startswith("deployed:"):
            return PromotionDecision(
                status=PromotionStatus.PROMOTE,
                reason="Run passed tests and policy gates under an auto-deployable policy.",
                evidence=base_evidence,
            )
        return PromotionDecision(
            status=PromotionStatus.NEEDS_REVIEW,
            reason=(
                "Run passed tests and policy gates; human review is required "
                "before merge or deploy."
            ),
            evidence=base_evidence,
        )

    @staticmethod
    def _safe_git_target(git_url: str | None) -> str | None:
        if not git_url:
            return None
        parsed = urllib.parse.urlparse(git_url)
        if parsed.scheme and parsed.netloc:
            return urllib.parse.urlunparse(
                (parsed.scheme, parsed.hostname or parsed.netloc, parsed.path, "", "", "")
            )
        if git_url.startswith("git@") and ":" in git_url:
            host, path = git_url.split(":", 1)
            return f"{host}:{path}"
        if parsed.scheme == "file":
            return "file://local-git-remote"
        return "git-remote"

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
            evidence=result.get("evidence", {}),
            promotion_decision=result.get("promotion_decision", {}),
        )

    def _fail(
        self,
        job_id: str,
        agent_result: dict[str, Any],
        gates: dict[str, bool],
        deployment_status: str,
    ) -> JobResult:
        promotion_decision = self._promotion_decision(
            status=JobStatus.FAILED,
            agent_result=agent_result,
            gates=gates,
            deployment_status=deployment_status,
            evidence={},
        )
        result = self._result(
            job_id=job_id,
            status=JobStatus.FAILED,
            agent_result=agent_result,
            gates=gates,
            pr_url=None,
            deployment_status=deployment_status,
            residual_risks=["Validation failed; branch was not synced or deployed."],
            evidence={},
            promotion_decision=promotion_decision,
        )
        self.store.update_job(job_id, status=JobStatus.FAILED, result_json=asdict(result))
        self.store.add_event(job_id, "job_failed", {"reason": deployment_status})
        self.store.add_event(
            job_id,
            "promotion_decision_created",
            asdict(promotion_decision),
        )
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
        evidence: dict[str, Any],
        promotion_decision: PromotionDecision,
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
            evidence=evidence,
            promotion_decision=asdict(promotion_decision),
        )

    @staticmethod
    def _job_id(request: JobRequest) -> str:
        payload = "|".join(
            [
                request.user_id,
                request.repo_provider.value,
                request.github_repo
                or request.git_url
                or str(Path(request.repo_path).expanduser().resolve()),
                request.model_id,
                request.agent_id,
                request.base_branch,
                request.prompt,
                os.urandom(8).hex(),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
