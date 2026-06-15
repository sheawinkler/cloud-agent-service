import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_agent_service.cloud_dispatch import EcsDispatchPlanner
from cloud_agent_service.dataset_export import SlmDatasetExporter
from cloud_agent_service.execution import ExecutionProvider
from cloud_agent_service.harness_registry import HarnessRegistry
from cloud_agent_service.models import (
    CloudDispatchStatus,
    DeploymentPolicy,
    JobRequest,
    JobStatus,
    RepoProvider,
    RoutingPolicy,
    WorkerCallbackType,
)
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
            self.assertEqual("local-template", result.evidence["harness_spec"]["harness_id"])
            self.assertEqual(
                "local-template.locked-down.v1",
                result.evidence["security_profile"]["profile_id"],
            )
            self.assertEqual(
                "local-template-adapter",
                result.evidence["harness_adapter_result"]["adapter_id"],
            )
            self.assertEqual(
                "executed",
                result.evidence["harness_adapter_result"]["adapter_status"],
            )
            self.assertTrue(result.evidence["run_artifact"]["complete"])
            self.assertTrue(result.policy_gate_results["artifact_policy"])
            self.assertTrue(result.policy_gate_results["transcript_policy"])
            self.assertTrue(result.policy_gate_results["security_profile_policy"])
            self.assertEqual("promote", result.promotion_decision["status"])

            workspace_index = root / "workspaces" / job_id / "repo" / "index.html"
            self.assertIn('data-agent="buy-button"', workspace_index.read_text(encoding="utf-8"))
            self.assertTrue((root / "artifacts" / f"{job_id}-pr.json").exists())
            self.assertTrue((root / "artifacts" / f"{job_id}-deployment.json").exists())
            self.assertTrue(
                (root / "artifacts" / "previews" / job_id / "browser-proof.json").exists()
            )
            self.assertTrue(Path(result.evidence["run_artifact"]["artifact_path"]).exists())
            self.assertTrue(Path(result.evidence["run_artifact"]["transcript_path"]).exists())
            self.assertTrue(Path(result.evidence["run_artifact"]["diff_path"]).exists())
            self.assertEqual(3, len(result.evidence["artifact_refs"]))
            self.assertEqual(3, len(flow.store.list_artifact_refs(job_id)))
            self.assertEqual("local", result.evidence["artifact_refs"][0]["provider"])
            self.assertEqual("local_mock", result.evidence["deployment_provider"]["provider"])
            self.assertEqual(
                "provenance-manifest.v1",
                result.evidence["provenance"]["schema_version"],
            )
            self.assertTrue(Path(result.evidence["provenance"]["path"]).exists())
            provenance_payload = json.loads(
                Path(result.evidence["provenance"]["path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(job_id, provenance_payload["job_id"])
            self.assertEqual("index.html", provenance_payload["source_fingerprints"][0]["path"])
            self.assertGreater(flow.store.budget_tokens_used(job_id), 0)
            lab_run = flow.store.get_lab_run(job_id)
            self.assertEqual("local-deterministic", lab_run["model_id"])
            self.assertEqual("repo-editor-v1", lab_run["agent_id"])
            self.assertEqual("local-template", lab_run["harness_id"])
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
            self.assertEqual(
                [{"harness_id": "local-template", "promotion_status": "promote", "count": 1}],
                summary["by_harness"],
            )
            leaderboard = flow.store.lab_leaderboard()
            self.assertEqual(1, len(leaderboard))
            self.assertEqual("local-template", leaderboard[0]["harness_id"])
            self.assertEqual(1, leaderboard[0]["promote_count"])
            self.assertEqual(1.0, leaderboard[0]["promotion_rate"])

            expected_events = {
                "job_created",
                "job_queued",
                "harness_selected",
                "agent_dispatched",
                "harness_adapter_selected",
                "harness_security_profile_selected",
                "repo_cloned",
                "repo_analyzed",
                "budget_charged",
                "prompt_upgraded",
                "plan_created",
                "dependencies_requested",
                "harness_adapter_finished",
                "files_changed",
                "tests_finished",
                "policy_gate_result",
                "preview_created",
                "browser_proof_finished",
                "run_artifact_created",
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
            self.assertEqual(f"local://jobs/{job_id}", payload.status_callback_url)
            self.assertEqual(RoutingPolicy.FIXED, payload.routing_policy)
            self.assertEqual("fixed", payload.routing_decision["routing_policy"])
            self.assertEqual("local-deterministic", payload.model_id)
            self.assertEqual("repo-editor-v1", payload.agent_id)
            self.assertEqual("local-template", payload.harness_id)
            self.assertEqual("deterministic-repo-editor", payload.model_spec["name"])
            self.assertEqual("repo_editor", payload.agent_spec["role"])
            self.assertEqual(
                "local-deterministic-edit.v1",
                payload.harness_spec["execution_contract"],
            )
            self.assertEqual(
                "local-template-adapter",
                payload.harness_adapter_contract["adapter_id"],
            )
            self.assertEqual(
                "local-template.locked-down.v1",
                payload.security_profile["profile_id"],
            )
            self.assertIn("policy_gate_results", payload.output_schema)

    def test_analysis_cases_experiment_report_and_dataset_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)

            cases = flow.list_analysis_cases()
            self.assertGreaterEqual(len(cases), 4)
            self.assertIsNotNone(flow.get_analysis_case("model_bakeoff_repo_edit"))

            spec = flow.create_analysis_experiment(
                case_id="model_bakeoff_repo_edit",
                name="local bakeoff smoke",
            )
            run = flow.run_analysis_experiment(
                spec.experiment_id,
                repo_path=str(repo),
                deploy_policy=DeploymentPolicy.PREVIEW_ONLY,
            )
            report = flow.experiment_report(spec.experiment_id)

            self.assertEqual(spec.experiment_id, run.experiment_id)
            self.assertEqual(1, len(run.job_ids))
            self.assertEqual(1, report.total_runs)
            self.assertEqual(1, report.by_promotion_status["needs_review"])
            self.assertEqual("human_review_required", run.analyses[0].failure_category)
            self.assertTrue(run.analyses[0].run_artifact_complete)

            export = flow.export_slm_dataset(export_id="test_export", limit=50)
            self.assertEqual("test_export", export.export_id)
            self.assertEqual(1, sum(export.counts.values()))
            self.assertTrue(Path(export.artifact_path).exists())
            exported = flow.get_dataset_export("test_export")
            self.assertEqual(export.counts, exported["counts"])
            records = []
            for split_path in export.split_paths.values():
                path = Path(split_path)
                self.assertTrue(path.exists())
                records.extend(
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line
                )
            self.assertEqual(1, len(records))
            self.assertEqual("slm-dataset.v1", records[0]["schema_version"])
            self.assertIn(records[0]["dataset_split"], {"train", "eval", "holdout"})
            self.assertIn("source_run_artifact", records[0])
            self.assertFalse(export.lineage["holdout_guard"]["use_for_training"])
            self.assertIn("source_fingerprints", export.lineage)
            self.assertNotIn(str(root), json.dumps(records[0], sort_keys=True))

    def test_analysis_experiment_batch_records_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            spec = flow.create_analysis_experiment(
                case_id="model_bakeoff_repo_edit",
                name="batch smoke",
            )

            batch_result = flow.run_analysis_experiment_batch(
                spec.experiment_id,
                repo_path=str(repo),
                deploy_policy=DeploymentPolicy.PREVIEW_ONLY,
                max_concurrency=2,
            )

            batch = batch_result["batch"]
            self.assertEqual(spec.experiment_id, batch["experiment_id"])
            self.assertEqual("completed", batch["status"])
            self.assertEqual(2, batch["max_concurrency"])
            self.assertEqual(1, batch["requested_jobs"])
            self.assertEqual(1, batch["completed_jobs"])
            stored = flow.get_analysis_experiment_batch(batch["batch_id"])
            self.assertEqual(batch["batch_id"], stored["batch_id"])
            self.assertEqual(batch["job_ids"], stored["job_ids"])

    def test_unknown_analysis_case_and_empty_dataset_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flow = self._build_flow(root)

            self.assertIsNone(flow.get_analysis_case("missing_case"))
            with self.assertRaises(RequestValidationError):
                flow.create_analysis_experiment(case_id="missing_case")

            export = flow.export_slm_dataset(export_id="empty_export", limit=50)
            self.assertEqual({"train": 0, "eval": 0, "holdout": 0}, export.counts)
            self.assertEqual([], export.source_job_ids)
            for split_path in export.split_paths.values():
                path = Path(split_path)
                self.assertTrue(path.exists())
                self.assertEqual("", path.read_text(encoding="utf-8"))

    def test_dataset_export_redacts_paths_and_secrets(self):
        redacted = SlmDatasetExporter._redact_text(
            "/Users/sheawinkler/private sk-testsecret OPENAI_API_KEY=abc123"
        )

        self.assertIn("<redacted_path>", redacted)
        self.assertIn("<redacted_secret>", redacted)
        self.assertNotIn("/Users/sheawinkler/private", redacted)
        self.assertNotIn("OPENAI_API_KEY=abc123", redacted)

    def test_router_recommend_and_auto_select_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)

            cold = flow.recommend_route(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    routing_policy=RoutingPolicy.RECOMMEND_ONLY,
                )
            )
            self.assertTrue(cold.fallback)
            self.assertIn("model_bakeoff_repo_edit", cold.nearest_analysis_cases)

            flow.run_job(
                flow.create_job(
                    JobRequest(
                        prompt="For my shopping website, create a buy button.",
                        repo_path=str(repo),
                        deploy_policy=DeploymentPolicy.LOCAL,
                    )
                )
            )

            warm = flow.recommend_route(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    routing_policy=RoutingPolicy.RECOMMEND_ONLY,
                )
            )
            self.assertFalse(warm.fallback)
            self.assertEqual("local-deterministic", warm.selected_model_id)
            self.assertEqual("repo-editor-v1", warm.selected_agent_id)
            self.assertEqual("local-template", warm.selected_harness_id)

            auto_job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    routing_policy=RoutingPolicy.AUTO_SELECT,
                )
            )
            auto_job = flow.store.get_job(auto_job_id)
            self.assertEqual("auto_select", auto_job["routing_policy"])
            self.assertEqual(
                "local-template",
                auto_job["routing_decision_json"]["selected_harness_id"],
            )

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

    def test_harness_registry_indexes_top_twenty_and_custom_contracts(self):
        registry = HarnessRegistry()
        top = registry.top()
        response = registry.response()
        custom = registry.get("custom:acme-runner")

        self.assertEqual(20, len(top))
        self.assertEqual(list(range(1, 21)), [harness.rank for harness in top])
        self.assertEqual(24, len(response["harnesses"]))
        top_ids = {harness.harness_id for harness in top}
        indexed_ids = {harness["harness_id"] for harness in response["harnesses"]}
        self.assertEqual("factory-droid", top[0].harness_id)
        self.assertTrue(
            {
                "factory-droid",
                "pi-coding-agent",
                "hermes-agent",
                "openai-codex-cli",
            }.issubset(top_ids)
        )
        self.assertTrue({"agno", "crewai", "llamaindex"}.issubset(indexed_ids))
        self.assertEqual("custom:acme-runner", custom.harness_id)
        self.assertEqual("custom-harness.v1", custom.execution_contract)

    def test_unknown_harness_is_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)

            with self.assertRaises(RequestValidationError):
                flow.create_job(
                    JobRequest(
                        prompt="For my shopping website, create a buy button.",
                        repo_path=str(repo),
                        harness_id="missing-harness",
                    )
                )

    def test_custom_harness_contract_is_preserved_in_payload_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            request = JobRequest(
                prompt="For my shopping website, create a buy button.",
                repo_path=str(repo),
                deploy_policy=DeploymentPolicy.LOCAL,
                harness_id="custom:acme-runner",
            )

            job_id = flow.create_job(request)
            payload = flow.build_worker_payload(job_id)
            result = flow.run_job(job_id)

            self.assertEqual("custom:acme-runner", payload.harness_id)
            self.assertEqual("custom-harness.v1", payload.harness_spec["execution_contract"])
            self.assertEqual("custom:acme-runner", result.evidence["harness_spec"]["harness_id"])
            self.assertEqual(
                "contract_fallback",
                result.evidence["harness_adapter_result"]["adapter_status"],
            )
            self.assertEqual("custom:acme-runner", flow.store.get_lab_run(job_id)["harness_id"])

    def test_pi_coding_agent_adapter_executes_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            script = root / "fake_pi_agent.py"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import pathlib\n"
                "import sys\n"
                "\n"
                "repo = pathlib.Path(sys.argv[sys.argv.index('--repo') + 1])\n"
                "result_path = pathlib.Path(sys.argv[sys.argv.index('--result') + 1])\n"
                "target = repo / 'index.html'\n"
                "content = target.read_text(encoding='utf-8')\n"
                "button = '<button type=\"button\" data-agent=\"buy-button\">Buy</button>'\n"
                "if button not in content:\n"
                "    content = content.replace('</body>', f'  {button}\\n</body>', 1)\n"
                "    target.write_text(content, encoding='utf-8')\n"
                "result_path.write_text(json.dumps({\n"
                "    'changed_files': ['index.html'],\n"
                "    'commands_run': ['fake pi adapter'],\n"
                "    'tests_passed': [],\n"
                "    'tests_failed': [],\n"
                "    'dependency_changes': [],\n"
                "    'residual_risks': [],\n"
                "    'transcript': ['fake pi adapter executed']\n"
                "}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            with patch.dict(
                "os.environ",
                {
                    "AGENT_CLOUD_ENABLE_PI_CODING_AGENT": "1",
                    "AGENT_CLOUD_PI_CODING_AGENT_CMD": str(script),
                },
            ):
                flow = self._build_flow(root)
                job_id = flow.create_job(
                    JobRequest(
                        prompt="For my shopping website, create a buy button.",
                        repo_path=str(repo),
                        deploy_policy=DeploymentPolicy.LOCAL,
                        harness_id="pi-coding-agent",
                    )
                )
                payload = flow.build_worker_payload(job_id)
                result = flow.run_job(job_id)

            self.assertEqual("pi-coding-agent", payload.harness_id)
            self.assertTrue(payload.harness_adapter_contract["enabled"])
            self.assertEqual(JobStatus.SUCCEEDED, result.status)
            self.assertEqual("promote", result.promotion_decision["status"])
            self.assertEqual(
                "pi-coding-agent-adapter",
                result.evidence["harness_adapter_result"]["adapter_id"],
            )
            self.assertEqual(
                "executed",
                result.evidence["harness_adapter_result"]["adapter_status"],
            )
            self.assertEqual(
                "pi-coding-agent.cli-adapter.v1",
                result.evidence["security_profile"]["profile_id"],
            )
            self.assertTrue(result.evidence["run_artifact"]["complete"])

    def test_job_store_migrates_legacy_lab_runs_before_harness_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "jobs.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE lab_runs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        repo_provider TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        job_status TEXT NOT NULL,
                        promotion_status TEXT NOT NULL,
                        promotion_reason TEXT NOT NULL,
                        deployment_status TEXT NOT NULL,
                        changed_files_count INTEGER NOT NULL,
                        tests_failed_count INTEGER NOT NULL,
                        token_budget INTEGER NOT NULL,
                        tokens_used INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX idx_lab_runs_model_agent_status
                    ON lab_runs (model_id, agent_id, promotion_status)
                    """
                )

            JobStore(db_path)

            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(lab_runs)").fetchall()
                }
                indexes = {
                    row[1]
                    for row in conn.execute("PRAGMA index_list(lab_runs)").fetchall()
                }

            self.assertIn("harness_id", columns)
            self.assertIn("idx_lab_runs_model_agent_harness_status", indexes)
            self.assertIn("idx_lab_runs_harness_status", indexes)

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
            payload = flow.build_worker_payload(
                job_id,
                status_callback_url="https://api.example.com/jobs",
            )

            plan = EcsDispatchPlanner().build_run_task_request(payload)

            request = plan["run_task_request"]
            self.assertEqual("aws-ecs", plan["provider"])
            self.assertEqual("dry-run-contract", plan["mode"])
            self.assertEqual("agent-cluster", request["cluster"])
            self.assertEqual("agent-task:1", request["taskDefinition"])
            awsvpc_config = request["networkConfiguration"]["awsvpcConfiguration"]
            container = request["overrides"]["containerOverrides"][0]
            env = {entry["name"]: entry["value"] for entry in container["environment"]}
            self.assertEqual(["subnet-a", "subnet-b"], awsvpc_config["subnets"])
            self.assertEqual("worker", container["name"])
            self.assertIn("--job-id", container["command"])
            self.assertEqual("local-template", env["AGENT_CLOUD_HARNESS_ID"])
            self.assertEqual(
                f"https://api.example.com/jobs/{job_id}",
                env["AGENT_CLOUD_STATUS_CALLBACK_URL"],
            )

    def test_cloud_dispatch_submit_is_env_gated_and_persisted(self):
        class FakeEcsClient:
            def run_task(self, **request):
                self.request = request
                return {
                    "tasks": [{"taskArn": "arn:aws:ecs:us-west-2:123:task/abc"}],
                    "failures": [],
                    "ResponseMetadata": {"HTTPStatusCode": 200},
                }

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "AGENT_CLOUD_ECS_CLUSTER": "agent-cluster",
                "AGENT_CLOUD_ECS_TASK_DEFINITION": "agent-task:1",
                "AGENT_CLOUD_ECS_SUBNETS": "subnet-a,subnet-b",
                "AGENT_CLOUD_ECS_SUBMIT_ENABLED": "1",
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
            fake_client = FakeEcsClient()

            dispatch = EcsDispatchPlanner().submit_run_task(payload, ecs_client=fake_client)
            flow.record_cloud_dispatch(dispatch)

            self.assertEqual(CloudDispatchStatus.SUBMITTED, dispatch.status)
            self.assertEqual("arn:aws:ecs:us-west-2:123:task/abc", dispatch.task_arn)
            self.assertEqual("agent-cluster", fake_client.request["cluster"])
            stored = flow.store.get_cloud_dispatch(dispatch.dispatch_id)
            self.assertEqual("submitted", stored["status"])
            self.assertEqual(job_id, stored["job_id"])

    def test_worker_callback_protocol_records_progress(self):
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

            flow.record_worker_callback(
                job_id,
                WorkerCallbackType.STARTED,
                "running",
                {"task_arn": "arn:task/test"},
            )
            flow.record_worker_callback(
                job_id,
                WorkerCallbackType.HEARTBEAT,
                "running",
                {"progress": "repo_cloned"},
            )

            callbacks = flow.list_worker_callbacks(job_id)
            self.assertEqual(2, len(callbacks))
            self.assertEqual("started", callbacks[0]["callback_type"])
            self.assertEqual(JobStatus.RUNNING.value, flow.store.get_job(job_id)["status"])

    def test_cloud_dispatch_status_reports_missing_ecs_configuration(self):
        with patch.dict("os.environ", {}, clear=True):
            status = EcsDispatchPlanner().status()

            self.assertFalse(status["configured"])
            self.assertIn("AGENT_CLOUD_ECS_CLUSTER", status["missing"])

    def test_duckdb_store_backend_runs_local_flow_when_available(self):
        if importlib.util.find_spec("duckdb") is None:
            self.skipTest("duckdb package is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = AgentCloudFlow(
                store=JobStore(root / "jobs.duckdb", provider="duckdb"),
                workspace_root=root / "workspaces",
                artifacts_dir=root / "artifacts",
            )
            self.assertEqual("duckdb", flow.store.status().provider)

            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    deploy_policy=DeploymentPolicy.LOCAL,
                )
            )
            result = flow.run_job(job_id)

            self.assertEqual(JobStatus.SUCCEEDED, result.status)
            self.assertEqual("promote", result.promotion_decision["status"])
            self.assertEqual(1, flow.store.lab_summary()["total_runs"])

    def test_vercel_preview_provider_records_dry_run_contract(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {"AGENT_CLOUD_DEPLOYMENT_PROVIDER": "vercel_preview"},
        ):
            root = Path(tmp)
            repo = self._build_repo(root)
            flow = self._build_flow(root)
            self.assertEqual("vercel_preview", flow.deployer.status().provider)

            job_id = flow.create_job(
                JobRequest(
                    prompt="For my shopping website, create a buy button.",
                    repo_path=str(repo),
                    deploy_policy=DeploymentPolicy.PREVIEW_ONLY,
                )
            )
            result = flow.run_job(job_id)

            self.assertEqual("ready: vercel preview contract recorded", result.deployment_status)
            self.assertEqual("vercel_preview", result.evidence["deployment_provider"]["provider"])
            self.assertTrue(Path(result.evidence["deployment_provider"]["artifact_path"]).exists())
            self.assertEqual("needs_review", result.promotion_decision["status"])

    def test_execution_provider_status_contract(self):
        with patch.dict("os.environ", {"AGENT_CLOUD_EXECUTION_PROVIDER": "vercel_sandbox"}):
            status = ExecutionProvider().status()

            self.assertEqual("vercel_sandbox", status.provider)
            self.assertEqual("sandbox-contract", status.mode)
            self.assertTrue(status.configured)

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
