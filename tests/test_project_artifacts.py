import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectArtifactTests(unittest.TestCase):
    def test_agent_contract_example_is_valid_json(self):
        contract_path = ROOT / "examples" / "agent_contract.json"
        payload = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertEqual("demo-job-001", payload["job_payload"]["job_id"])
        self.assertEqual("succeeded", payload["final_result"]["status"])
        self.assertIn("policy_gate_results", payload["final_result"])
        self.assertIn("evidence", payload["final_result"])
        self.assertEqual(
            "event-intake-result.v1",
            payload["event_intake_example"]["schema_version"],
        )
        self.assertEqual("sota-readiness.v1", payload["readiness_example"]["schema_version"])
        self.assertIn("git_url", payload["job_payload"])
        self.assertIn("github_repo", payload["job_payload"])
        self.assertIn("model_spec", payload["job_payload"])
        self.assertIn("agent_spec", payload["job_payload"])
        self.assertIn("harness_spec", payload["job_payload"])
        self.assertIn("harness_adapter_contract", payload["job_payload"])
        self.assertIn("security_profile", payload["job_payload"])
        self.assertIn("routing_policy", payload["job_payload"])
        self.assertIn("routing_decision", payload["job_payload"])
        self.assertIn("callback_auth", payload["job_payload"])
        self.assertEqual("local-template", payload["job_payload"]["harness_id"])
        self.assertIn("harness_spec", payload["final_result"]["evidence"])
        self.assertIn("routing_decision", payload["final_result"]["evidence"])
        self.assertIn("review_forge", payload["final_result"]["evidence"])
        self.assertIn("harness_adapter_result", payload["final_result"]["evidence"])
        self.assertIn("security_profile", payload["final_result"]["evidence"])
        self.assertIn("run_artifact", payload["final_result"]["evidence"])
        self.assertIn("artifact_refs", payload["final_result"]["evidence"])
        self.assertIn("deployment_provider", payload["final_result"]["evidence"])
        self.assertIn("provenance", payload["final_result"]["evidence"])
        self.assertIn("promotion_decision", payload["final_result"])
        self.assertIn(
            "promotion_evaluation",
            payload["final_result"]["promotion_decision"]["evidence"],
        )

    def test_demo_local_flow_succeeds(self):
        result = subprocess.run(
            ["./demo.sh", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual("succeeded", payload["status"])
        self.assertEqual([], payload["tests_failed"])
        self.assertTrue(payload["browser_checks"]["buy_button_present"])
        self.assertIn("job_succeeded", payload["events"])

    def test_evaluation_harness_scores_buy_button_task(self):
        result = subprocess.run(
            ["python3", "scripts/evaluate_mvp.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual("shopping_buy_button", payload["task"])
        self.assertEqual(1.0, payload["score"])
        self.assertTrue(payload["checks"]["buy_button_present"])
        self.assertTrue(payload["checks"]["preview_artifact_created"])

    def test_task_suite_scores_multiple_policy_outcomes(self):
        result = subprocess.run(
            ["python3", "scripts/evaluate_task_suite.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual("repo_edit_replay_corpus_v1", payload["suite_id"])
        self.assertEqual(1.0, payload["score"])
        self.assertEqual(10, len(payload["tasks"]))
        self.assertEqual(10, payload["lab_summary"]["total_runs"])
        self.assertGreaterEqual(len(payload["leaderboard"]), 1)
        self.assertIn("promote", payload["lab_summary"]["by_promotion_status"])
        self.assertIn("needs_review", payload["lab_summary"]["by_promotion_status"])
        self.assertIn("reject", payload["lab_summary"]["by_promotion_status"])

    def test_api_smoke_script_has_standard_entrypoint(self):
        script = ROOT / "scripts" / "smoke_api.py"
        content = script.read_text(encoding="utf-8")

        self.assertIn("def run_smoke", content)
        self.assertIn("if __name__ == \"__main__\"", content)

    def test_dataset_export_script_has_standard_entrypoint(self):
        script = ROOT / "scripts" / "export_slm_dataset.py"
        content = script.read_text(encoding="utf-8")

        self.assertIn("def export_dataset", content)
        self.assertIn("if __name__ == \"__main__\"", content)

    def test_doctor_script_and_readiness_doc_exist(self):
        script = ROOT / "scripts" / "doctor.py"
        docs = ROOT / "docs" / "sota-feature-readiness.md"
        script_content = script.read_text(encoding="utf-8")
        docs_content = docs.read_text(encoding="utf-8")

        self.assertIn("def main", script_content)
        self.assertIn("ReadinessReporter", script_content)
        self.assertIn("/readiness/scorecard", docs_content)
        self.assertIn("/events/intake", docs_content)

    def test_lab_in_a_box_demo_succeeds(self):
        result = subprocess.run(
            ["python3", "scripts/demo_lab_in_a_box.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual("lab-in-a-box-demo.v1", payload["schema_version"])
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["checks"]["dataset_exported"])
        self.assertTrue(payload["checks"]["router_recommended"])
        self.assertTrue(payload["checks"]["readiness_reported"])
        self.assertIn("readiness_score", payload["readiness"])


if __name__ == "__main__":
    unittest.main()
