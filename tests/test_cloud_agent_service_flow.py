import tempfile
import unittest
from pathlib import Path

from cloud_agent_service.models import DeploymentPolicy, JobRequest, JobStatus
from cloud_agent_service.pipeline import (
    AgentCloudFlow,
    DependencyInstaller,
    Planner,
    PromptUpgrader,
    RequestValidationError,
    RequestValidator,
)
from cloud_agent_service.store import JobStore


class CloudAgentServiceFlowTests(unittest.TestCase):
    def _build_flow(self, root: Path) -> AgentCloudFlow:
        return AgentCloudFlow(
            store=JobStore(root / "jobs.sqlite3"),
            workspace_root=root / "workspaces",
            artifacts_dir=root / "artifacts",
        )

    def _build_repo(self, root: Path) -> Path:
        repo = root / "target_repo"
        repo.mkdir()
        (repo / "index.html").write_text(
            "<!doctype html>\n<html>\n<body>\n<h1>Shop</h1>\n</body>\n</html>\n",
            encoding="utf-8",
        )
        return repo

    def test_rejects_oversized_prompt_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._build_repo(Path(tmp))
            request = JobRequest(
                prompt="x" * 11,
                repo_path=str(repo),
                max_prompt_chars=10,
            )

            with self.assertRaises(RequestValidationError):
                RequestValidator().validate(request)

    def test_successful_local_flow_covers_request_to_final_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            request = JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_path=str(repo),
                deploy_policy=DeploymentPolicy.LOCAL,
            )

            job_id = flow.create_job(request)
            result = flow.run_job(job_id)
            job = flow.store.get_job(job_id)
            events = [event["event_type"] for event in flow.store.list_events(job_id)]

            self.assertEqual(JobStatus.SUCCEEDED, result.status)
            self.assertEqual(JobStatus.SUCCEEDED.value, job["status"])
            self.assertIn("index.html", result.changed_files)
            self.assertTrue(all(result.policy_gate_results.values()))
            self.assertEqual([], result.tests_failed)
            self.assertIn("python3 -m compileall .", result.tests_passed)
            self.assertEqual(f"local://github/pr/{job_id}", result.pr_url)
            self.assertEqual("deployed: local mock deployment recorded", result.deployment_status)

            workspace_index = root / "workspaces" / job_id / "repo" / "index.html"
            self.assertIn('data-agent="buy-button"', workspace_index.read_text(encoding="utf-8"))
            self.assertTrue((root / "artifacts" / f"{job_id}-pr.json").exists())
            self.assertTrue((root / "artifacts" / f"{job_id}-deployment.json").exists())

            expected_events = {
                "job_created",
                "job_queued",
                "agent_dispatched",
                "repo_cloned",
                "prompt_upgraded",
                "plan_created",
                "dependencies_requested",
                "files_changed",
                "tests_finished",
                "policy_gate_result",
                "branch_pushed",
                "pr_created_or_updated",
                "deployment_finished",
                "job_succeeded",
            }
            self.assertTrue(expected_events.issubset(set(events)))

    def test_dependency_installer_keeps_allowlist_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._build_repo(Path(tmp))
            request = JobRequest(prompt="Create a buy button", repo_path=str(repo))
            normalized = PromptUpgrader().upgrade(request)
            plan = Planner().create_plan(request, normalized)

            installer = DependencyInstaller()
            modules = installer.requested_modules(plan)
            command = installer.install_command(modules)

            self.assertEqual(["pytest", "ruff"], modules)
            self.assertEqual("scripts/install_allowed_modules.sh pytest ruff", command)


if __name__ == "__main__":
    unittest.main()
