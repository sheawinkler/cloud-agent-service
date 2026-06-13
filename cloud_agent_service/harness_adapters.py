from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cloud_agent_service.models import AgentPlan, HarnessSpec
from cloud_agent_service.security_profiles import HarnessSecurityProfile


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HarnessAdapterContract:
    adapter_id: str
    harness_id: str
    mode: str
    command_template: list[str]
    input_contract: str
    output_contract: str
    enabled: bool
    notes: list[str]


@dataclass(frozen=True)
class HarnessExecutionRequest:
    job_id: str
    repo_path: Path
    plan: AgentPlan
    harness_spec: HarnessSpec
    security_profile: HarnessSecurityProfile
    artifacts_dir: Path
    max_runtime_seconds: int


@dataclass
class HarnessExecutionResult:
    adapter_id: str
    adapter_status: str
    changed_files: list[str]
    commands_run: list[str]
    tests_passed: list[str]
    tests_failed: list[str]
    dependency_changes: list[str]
    residual_risks: list[str]
    transcript: list[str]
    raw_artifacts: dict[str, Any]

    def agent_result(self) -> dict[str, Any]:
        return {
            "changed_files": self.changed_files,
            "commands_run": self.commands_run,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "dependency_changes": self.dependency_changes,
            "residual_risks": self.residual_risks,
            "adapter_result": asdict(self),
        }


class LocalTemplateAdapter:
    adapter_id = "local-template-adapter"

    def contract_for(self, harness: HarnessSpec) -> HarnessAdapterContract:
        mode = (
            "local-deterministic"
            if harness.harness_id == "local-template"
            else "contract-fallback"
        )
        adapter_id = (
            self.adapter_id
            if harness.harness_id == "local-template"
            else "local-fallback-adapter"
        )
        return HarnessAdapterContract(
            adapter_id=adapter_id,
            harness_id=harness.harness_id,
            mode=mode,
            command_template=[],
            input_contract="harness_execution_request.v1",
            output_contract="harness_execution_result.v1",
            enabled=True,
            notes=[
                "Executes deterministic local templates.",
                (
                    "For non-local harness IDs this is a contract fallback, "
                    "not live harness execution."
                ),
            ],
        )

    def execute(self, request: HarnessExecutionRequest) -> HarnessExecutionResult:
        repo = request.repo_path
        changed_files: list[str] = []
        lower = request.plan.normalized_prompt.lower()
        if "buy button" in lower or ("buy" in lower and "button" in lower):
            changed_files.extend(self._add_buy_button(repo))
            action = "added deterministic buy button"
        else:
            changed_files.append(self._write_agent_note(repo, request.plan))
            action = "wrote deterministic implementation note"
        fallback = request.harness_spec.harness_id != "local-template"
        return HarnessExecutionResult(
            adapter_id=self.adapter_id if not fallback else "local-fallback-adapter",
            adapter_status="executed" if not fallback else "contract_fallback",
            changed_files=sorted(set(changed_files)),
            commands_run=[],
            tests_passed=[],
            tests_failed=[],
            dependency_changes=[],
            residual_risks=[]
            if not fallback
            else [
                "Selected harness is recorded as a dispatch contract; local fallback executed "
                "because no live adapter is configured."
            ],
            transcript=[
                f"adapter={self.adapter_id}",
                f"harness_id={request.harness_spec.harness_id}",
                f"action={action}",
            ],
            raw_artifacts={"deterministic_template": True, "fallback": fallback},
        )

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

    @staticmethod
    def _write_agent_note(repo: Path, plan: AgentPlan) -> str:
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


class PiCodingAgentAdapter:
    adapter_id = "pi-coding-agent-adapter"

    def __init__(self, command: str | None = None, enabled: bool | None = None) -> None:
        self.command = command or os.environ.get(
            "AGENT_CLOUD_PI_CODING_AGENT_CMD",
            "pi-coding-agent",
        )
        self.enabled = (
            _truthy_env("AGENT_CLOUD_ENABLE_PI_CODING_AGENT")
            or _truthy_env("AGENT_CLOUD_ENABLE_EXTERNAL_HARNESS")
            if enabled is None
            else enabled
        )

    def available(self) -> bool:
        executable = shlex.split(self.command)[0]
        return self.enabled and bool(shutil.which(executable))

    def contract(self) -> HarnessAdapterContract:
        return HarnessAdapterContract(
            adapter_id=self.adapter_id,
            harness_id="pi-coding-agent",
            mode="external-cli",
            command_template=[
                *shlex.split(self.command),
                "--repo",
                "<repo_path>",
                "--prompt",
                "<prompt>",
                "--result",
                "<result_json>",
            ],
            input_contract="harness_execution_request.v1",
            output_contract="harness_execution_result.v1",
            enabled=self.available(),
            notes=[
                "Runs only when AGENT_CLOUD_ENABLE_PI_CODING_AGENT or "
                "AGENT_CLOUD_ENABLE_EXTERNAL_HARNESS is truthy.",
                "The command must write harness_execution_result.v1 JSON to --result.",
            ],
        )

    def execute(self, request: HarnessExecutionRequest) -> HarnessExecutionResult:
        if not self.available():
            raise RuntimeError("pi-coding-agent adapter is not enabled or executable")
        result_path = request.artifacts_dir / "adapter-results" / f"{request.job_id}-pi.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            *shlex.split(self.command),
            "--repo",
            str(request.repo_path),
            "--prompt",
            request.plan.normalized_prompt,
            "--result",
            str(result_path),
        ]
        env = {
            **os.environ,
            "AGENT_CLOUD_JOB_ID": request.job_id,
            "AGENT_CLOUD_HARNESS_ID": request.harness_spec.harness_id,
            "AGENT_CLOUD_HARNESS_RESULT": str(result_path),
        }
        result = subprocess.run(
            command,
            cwd=request.repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=request.max_runtime_seconds,
            env=env,
        )
        transcript = [
            "$ " + " ".join(command),
            "stdout:",
            result.stdout[-4_000:],
            "stderr:",
            result.stderr[-4_000:],
        ]
        if result.returncode != 0:
            return HarnessExecutionResult(
                adapter_id=self.adapter_id,
                adapter_status="failed",
                changed_files=[],
                commands_run=[" ".join(command)],
                tests_passed=[],
                tests_failed=["pi-coding-agent adapter command"],
                dependency_changes=[],
                residual_risks=["Pi coding agent command failed before producing a valid result."],
                transcript=transcript,
                raw_artifacts={"returncode": result.returncode, "result_path": str(result_path)},
            )
        payload = (
            json.loads(result_path.read_text(encoding="utf-8"))
            if result_path.exists()
            else {}
        )
        return HarnessExecutionResult(
            adapter_id=self.adapter_id,
            adapter_status="executed",
            changed_files=sorted(set(payload.get("changed_files", []))),
            commands_run=[" ".join(command), *payload.get("commands_run", [])],
            tests_passed=list(payload.get("tests_passed", [])),
            tests_failed=list(payload.get("tests_failed", [])),
            dependency_changes=list(payload.get("dependency_changes", [])),
            residual_risks=list(payload.get("residual_risks", [])),
            transcript=[*transcript, *payload.get("transcript", [])],
            raw_artifacts={"returncode": result.returncode, "result_path": str(result_path)},
        )


class HarnessAdapterRegistry:
    def __init__(self) -> None:
        self.local = LocalTemplateAdapter()
        self.pi = PiCodingAgentAdapter()

    def contract_for(self, harness: HarnessSpec) -> HarnessAdapterContract:
        if harness.harness_id == "pi-coding-agent":
            return self.pi.contract()
        return self.local.contract_for(harness)

    def execute(self, request: HarnessExecutionRequest) -> HarnessExecutionResult:
        if request.harness_spec.harness_id == "pi-coding-agent" and self.pi.available():
            return self.pi.execute(request)
        return self.local.execute(request)
