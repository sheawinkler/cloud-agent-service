import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_agent_service.cloud_dispatch import EcsDispatchPlanner
from cloud_agent_service.models import DeploymentPolicy, JobRequest, JobStatus, RepoProvider
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

    def _build_bare_git_remote(self, root: Path) -> Path:
        source = self._build_repo(root)
        subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "add", "index.html"], cwd=source, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial site"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        remote = root / "remote.git"
        subprocess.run(
            ["git", "clone", "--bare", str(source), str(remote)],
            check=True,
            capture_output=True,
        )
        return remote

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

    def test_rejects_git_url_with_embedded_credentials(self):
        request = JobRequest(
            prompt="For my shopping website, create a buy button.",
            repo_provider=RepoProvider.GIT,
            git_url="https://user:token@git.example.com/owner/shop.git",
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
            self.assertEqual(
                "local://preview/" + job_id + "/index.html",
                result.evidence["preview_url"],
            )
            self.assertTrue(result.evidence["browser_checks"]["buy_button_present"])
            self.assertEqual("local-deterministic", result.evidence["model_spec"]["model_id"])
            self.assertEqual("repo-editor-v1", result.evidence["agent_spec"]["agent_id"])
            self.assertEqual("promote", result.promotion_decision["status"])

            workspace_index = root / "workspaces" / job_id / "repo" / "index.html"
            self.assertIn('data-agent="buy-button"', workspace_index.read_text(encoding="utf-8"))
            self.assertTrue((root / "artifacts" / f"{job_id}-pr.json").exists())
            self.assertTrue((root / "artifacts" / f"{job_id}-deployment.json").exists())
            self.assertTrue(
                (root / "artifacts" / "previews" / job_id / "browser-proof.json").exists()
            )
            self.assertGreater(flow.store.budget_tokens_used(job_id), 0)
            lab_run = flow.store.get_lab_run(job_id)
            self.assertEqual("local-deterministic", lab_run["model_id"])
            self.assertEqual("repo-editor-v1", lab_run["agent_id"])
            self.assertEqual("promote", lab_run["promotion_status"])
            self.assertEqual(1, lab_run["changed_files_count"])
            summary = flow.store.lab_summary()
            self.assertEqual(1, summary["total_runs"])
            self.assertEqual(1, summary["by_promotion_status"]["promote"])
            self.assertEqual(
                [
                    {
                        "model_id": "local-deterministic",
                        "agent_id": "repo-editor-v1",
                        "promotion_status": "promote",
                        "count": 1,
                    }
                ],
                summary["by_model_agent"],
            )

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
                "preview_created",
                "browser_proof_finished",
                "branch_pushed",
                "pr_created_or_updated",
                "deployment_finished",
                "job_succeeded",
                "repo_memory_loaded",
                "lab_run_configured",
                "promotion_decision_created",
            }
            self.assertTrue(expected_events.issubset(set(events)))
            repo_key = f"local:{repo.resolve()}"
            self.assertEqual(job_id, flow.store.get_repo_memory(repo_key)["last_job_id"])

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
            self.assertEqual("local-deterministic", payload.model_id)
            self.assertEqual("repo-editor-v1", payload.agent_id)
            self.assertEqual("deterministic-repo-editor", payload.model_spec["name"])
            self.assertEqual("repo_editor", payload.agent_spec["role"])
            self.assertIn("policy_gate_results", payload.output_schema)

    def test_unknown_model_or_agent_is_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)

            with self.assertRaises(RequestValidationError):
                flow.create_job(
                    JobRequest(
                        prompt="For my shopping website, create a buy button.",
                        repo_path=str(repo),
                        model_id="missing-model",
                    )
                )

            with self.assertRaises(RequestValidationError):
                flow.create_job(
                    JobRequest(
                        prompt="For my shopping website, create a buy button.",
                        repo_path=str(repo),
                        agent_id="missing-agent",
                    )
                )

    def test_model_agent_mismatch_is_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)

            with self.assertRaises(RequestValidationError):
                flow.create_job(
                    JobRequest(
                        prompt="For my shopping website, create a buy button.",
                        repo_path=str(repo),
                        model_id="gpt-5-coding",
                        agent_id="repo-editor-v1",
                    )
                )

    def test_github_worker_payload_uses_github_provider_without_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = self._build_flow(Path(tmp))
            request = JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_provider=RepoProvider.GITHUB,
                github_repo="owner/shop",
                token_budget=1234,
            )

            job_id = flow.create_job(request)
            payload = flow.build_worker_payload(job_id)

            self.assertEqual("github", payload.repo_provider)
            self.assertEqual("owner/shop", payload.github_repo)
            self.assertEqual("", payload.repo_path)
            self.assertEqual("agent/" + job_id, payload.working_branch)

    def test_generic_git_worker_payload_uses_git_url_without_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            flow = self._build_flow(Path(tmp))
            request = JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_provider=RepoProvider.GIT,
                git_url="https://git.example.com/owner/shop.git",
                token_budget=1234,
            )

            job_id = flow.create_job(request)
            payload = flow.build_worker_payload(job_id)

            self.assertEqual("git", payload.repo_provider)
            self.assertEqual("https://git.example.com/owner/shop.git", payload.git_url)
            self.assertEqual("", payload.repo_path)
            self.assertEqual("agent/" + job_id, payload.working_branch)

    def test_generic_git_flow_clones_and_pushes_review_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = self._build_bare_git_remote(root)
            flow = self._build_flow(root)
            request = JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_provider=RepoProvider.GIT,
                git_url=f"file://{remote}",
                deploy_policy=DeploymentPolicy.PR_ONLY,
            )

            job_id = flow.create_job(request)
            result = flow.run_job(job_id)

            self.assertEqual(JobStatus.SUCCEEDED, result.status)
            self.assertEqual(f"git://review/agent/{job_id}", result.pr_url)
            self.assertEqual("git", result.evidence["repo_provider"])
            self.assertEqual("file://local-git-remote", result.evidence["git_target"])
            self.assertEqual("needs_review", result.promotion_decision["status"])
            subprocess.run(
                ["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/agent/{job_id}"],
                check=True,
                capture_output=True,
            )

    def test_continuation_job_reuses_parent_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            parent_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                )
            )
            child_id = flow.create_job(
                JobRequest(
                    prompt="Make the buy button blue too.",
                    repo_path=str(repo),
                    parent_job_id=parent_id,
                )
            )

            parent = flow.store.get_job(parent_id)
            child = flow.store.get_job(child_id)
            self.assertEqual(parent["working_branch"], child["working_branch"])

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
            self.assertEqual("needs_review", result.promotion_decision["status"])
            self.assertEqual("needs_review", flow.store.get_lab_run(job_id)["promotion_status"])
            self.assertFalse((root / "artifacts" / f"{job_id}-deployment.json").exists())

            approved = flow.approve_deployment(job_id)
            events = [event["event_type"] for event in flow.store.list_events(job_id)]

            self.assertEqual("deployed: local mock deployment recorded", approved.deployment_status)
            self.assertEqual("promote", approved.promotion_decision["status"])
            self.assertEqual("promote", flow.store.get_lab_run(job_id)["promotion_status"])
            self.assertTrue((root / "artifacts" / f"{job_id}-deployment.json").exists())
            self.assertIn("deployment_approved", events)

    def test_deployment_policy_matrix_has_distinct_local_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            cases = {
                DeploymentPolicy.PR_ONLY: "skipped: PR only",
                DeploymentPolicy.PREVIEW_ONLY: "ready: preview only",
                DeploymentPolicy.STAGING_AUTO: "deployed: local staging mock deployment recorded",
                DeploymentPolicy.PRODUCTION_APPROVAL: "ready: manual approval required",
            }

            for policy, expected in cases.items():
                job_id = flow.create_job(
                    JobRequest(
                        prompt="For my shopping website, create a buy button.",
                        repo_path=str(repo),
                        deploy_policy=policy,
                    )
                )
                result = flow.run_job(job_id)
                self.assertEqual(expected, result.deployment_status)

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
            self.assertEqual("reject", result.promotion_decision["status"])
            self.assertEqual("reject", flow.store.get_lab_run(job_id)["promotion_status"])
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

    def test_github_status_reports_ready_when_app_env_is_present(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "GITHUB_APP_ID": "123",
                "GITHUB_APP_INSTALLATION_ID": "456",
                "GITHUB_APP_PRIVATE_KEY": "fake-key",
            },
        ):
            flow = self._build_flow(Path(tmp))
            status = flow.github_status()

            self.assertTrue(status.configured)
            self.assertEqual("ready", status.mode)

    def test_user_usage_tracks_reserved_budget_and_used_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    deploy_policy=DeploymentPolicy.LOCAL,
                    token_budget=3000,
                )
            )

            flow.run_job(job_id)
            usage = flow.store.user_usage("local-user")

            self.assertEqual("local-user", usage["user_id"])
            self.assertEqual(1, usage["jobs_count"])
            self.assertEqual(0, usage["active_jobs_count"])
            self.assertEqual(3000, usage["token_budget_reserved"])
            self.assertGreater(usage["tokens_used"], 0)

    def test_cloud_dispatch_plan_builds_ecs_run_task_contract(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "AGENT_CLOUD_ECS_CLUSTER": "agent-cluster",
                "AGENT_CLOUD_ECS_TASK_DEFINITION": "agent-task:1",
                "AGENT_CLOUD_ECS_SUBNETS": "subnet-a,subnet-b",
                "AGENT_CLOUD_ECS_SECURITY_GROUPS": "sg-1",
                "AGENT_CLOUD_ECS_CONTAINER_NAME": "worker",
                "AWS_REGION": "us-west-2",
            },
        ):
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                )
            )
            payload = flow.build_worker_payload(job_id)

            plan = EcsDispatchPlanner().build_run_task_request(payload)

            request = plan["run_task_request"]
            self.assertEqual("aws-ecs", plan["provider"])
            self.assertEqual("dry-run-contract", plan["mode"])
            self.assertEqual("agent-cluster", request["cluster"])
            self.assertEqual("agent-task:1", request["taskDefinition"])
            awsvpc_config = request["networkConfiguration"]["awsvpcConfiguration"]
            container = request["overrides"]["containerOverrides"][0]
            self.assertEqual(["subnet-a", "subnet-b"], awsvpc_config["subnets"])
            self.assertEqual("worker", container["name"])
            self.assertIn("--job-id", container["command"])

    def test_cloud_dispatch_status_reports_missing_ecs_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            status = EcsDispatchPlanner().status()

            self.assertFalse(status["configured"])
            self.assertIn("AGENT_CLOUD_ECS_CLUSTER", status["missing"])

    def test_openai_model_agent_path_requires_explicit_runtime_enablement(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "AGENT_CLOUD_ENABLE_OPENAI_AGENT": "",
                "OPENAI_API_KEY": "",
            },
        ):
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    model_id="gpt-5-coding",
                    agent_id="openai-repo-editor-v1",
                )
            )

            with self.assertRaises(RequestValidationError):
                flow.run_job(job_id)

            status = flow.model_agent_status()
            openai_model = next(
                model for model in status["models"] if model["model_id"] == "gpt-5-coding"
            )
            self.assertFalse(openai_model["runtime"]["configured"])

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
