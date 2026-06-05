import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            self.assertIn("protected_path_policy", result.policy_gate_results)

            workspace_index = root / "workspaces" / job_id / "repo" / "index.html"
            self.assertIn('data-agent="buy-button"', workspace_index.read_text(encoding="utf-8"))
            self.assertTrue((root / "artifacts" / f"{job_id}-pr.json").exists())
            self.assertTrue((root / "artifacts" / f"{job_id}-deployment.json").exists())
            self.assertGreater(flow.store.budget_tokens_used(job_id), 0)

            expected_events = {
                "job_created",
                "job_queued",
                "agent_dispatched",
                "repo_cloned",
                "repo_analyzed",
                "budget_charged",
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

    def test_worker_payload_preserves_intake_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            request = JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_path=str(repo),
                token_budget=1234,
                max_runtime_seconds=77,
                max_changed_files=1,
            )

            job_id = flow.create_job(request)
            payload = flow.build_worker_payload(job_id, status_callback_url="local://jobs")

            self.assertEqual(job_id, payload.job_id)
            self.assertEqual("agent/" + job_id, payload.working_branch)
            self.assertEqual(1234, payload.token_budget)
            self.assertEqual(77, payload.max_runtime_seconds)
            self.assertEqual(1, payload.max_changed_files)
            self.assertIn("policy_gate_results", payload.output_schema)

    def test_persisted_diff_policy_limit_blocks_sync_and_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            request = JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_path=str(repo),
                deploy_policy=DeploymentPolicy.LOCAL,
                max_changed_files=0,
            )

            job_id = flow.create_job(request)
            result = flow.run_job(job_id)

            self.assertEqual(JobStatus.FAILED, result.status)
            self.assertFalse(result.policy_gate_results["diff_policy"])
            self.assertIsNone(result.pr_url)
            self.assertEqual("not deployed: policy gate failed", result.deployment_status)
            self.assertFalse((root / "artifacts" / f"{job_id}-pr.json").exists())
            self.assertFalse((root / "artifacts" / f"{job_id}-deployment.json").exists())

    def test_manual_deployment_requires_approval_then_writes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    deploy_policy=DeploymentPolicy.MANUAL,
                )
            )

            result = flow.run_job(job_id)
            self.assertEqual("ready: manual approval required", result.deployment_status)
            self.assertFalse((root / "artifacts" / f"{job_id}-deployment.json").exists())

            approved = flow.approve_deployment(job_id)
            events = [event["event_type"] for event in flow.store.list_events(job_id)]

            self.assertEqual("deployed: local mock deployment recorded", approved.deployment_status)
            self.assertTrue((root / "artifacts" / f"{job_id}-deployment.json").exists())
            self.assertIn("deployment_approved", events)

    def test_tiny_budget_stops_before_sync_and_deploy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    deploy_policy=DeploymentPolicy.LOCAL,
                    token_budget=10,
                )
            )

            result = flow.run_job(job_id)
            events = [event["event_type"] for event in flow.store.list_events(job_id)]

            self.assertEqual(JobStatus.FAILED, result.status)
            self.assertEqual("not deployed: budget exceeded", result.deployment_status)
            self.assertIn("budget_exceeded", events)
            self.assertIsNone(result.pr_url)

    def test_claim_next_queued_job_runs_without_in_memory_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    deploy_policy=DeploymentPolicy.LOCAL,
                )
            )

            result = flow.run_next_queued_job()

            self.assertIsNotNone(result)
            self.assertEqual(job_id, result.job_id)
            self.assertEqual(JobStatus.SUCCEEDED, result.status)
            self.assertIsNone(flow.run_next_queued_job())

    def test_cancelled_queued_job_does_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                )
            )

            self.assertTrue(flow.cancel_job(job_id))

            job = flow.store.get_job(job_id)
            events = [event["event_type"] for event in flow.store.list_events(job_id)]
            self.assertEqual(JobStatus.CANCELLED.value, job["status"])
            self.assertIn("job_cancelled", events)
            self.assertIsNone(flow.run_next_queued_job())

    def test_failed_or_cancelled_job_can_be_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                )
            )

            self.assertTrue(flow.cancel_job(job_id))
            self.assertTrue(flow.retry_job(job_id))

            job = flow.store.get_job(job_id)
            events = [event["event_type"] for event in flow.store.list_events(job_id)]
            self.assertEqual(JobStatus.QUEUED.value, job["status"])
            self.assertIn("job_retried", events)

    def test_github_status_reports_missing_real_app_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "GITHUB_APP_ID": "",
                "GITHUB_APP_INSTALLATION_ID": "",
                "GITHUB_APP_PRIVATE_KEY": "",
            },
        ):
            flow = self._build_flow(Path(tmp))
            status = flow.github_status()

            self.assertFalse(status.configured)
            self.assertEqual("github-app", status.provider)
            self.assertIn("GITHUB_APP_ID", status.missing)

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
