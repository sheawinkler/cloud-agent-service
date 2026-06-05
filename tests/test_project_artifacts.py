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


if __name__ == "__main__":
    unittest.main()
