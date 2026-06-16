from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FeatureCapability:
    capability_id: str
    title: str
    category: str
    target: str
    status: str
    evidence: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    critical: bool = False


class ReadinessReporter:
    schema_version = "sota-readiness.v1"

    def __init__(self, flow: Any, ecs_dispatch_planner: Any) -> None:
        self.flow = flow
        self.ecs_dispatch_planner = ecs_dispatch_planner

    def report(self) -> dict[str, Any]:
        capabilities = self.capabilities()
        counts: dict[str, int] = {}
        for capability in capabilities:
            counts[capability.status] = counts.get(capability.status, 0) + 1
        weighted_total = sum(self._weight(capability) for capability in capabilities)
        weighted_ready = sum(
            self._weight(capability) * self._status_score(capability.status)
            for capability in capabilities
        )
        critical_blockers = [
            capability.capability_id
            for capability in capabilities
            if capability.critical and capability.status not in {"ready", "local_ready"}
        ]
        return {
            "schema_version": self.schema_version,
            "readiness_score": round(weighted_ready / weighted_total, 4)
            if weighted_total
            else 0.0,
            "production_ready": not critical_blockers,
            "status_counts": counts,
            "critical_blockers": critical_blockers,
            "capabilities": [capability.__dict__ for capability in capabilities],
            "feature_list": self.feature_list(capabilities),
            "notes": [
                "ready/local_ready means the repo has executable evidence for this boundary.",
                "env_gated means code exists but live behavior requires explicit credentials "
                "or flags.",
                "contract means the shape is modeled but provider-native mutation is not "
                "claimed live.",
            ],
        }

    def feature_list(
        self,
        capabilities: list[FeatureCapability] | None = None,
    ) -> list[dict[str, Any]]:
        items = capabilities or self.capabilities()
        return [
            {
                "id": capability.capability_id,
                "feature": capability.title,
                "category": capability.category,
                "status": capability.status,
                "why_it_matters": capability.target,
                "next": capability.next_steps[:2],
            }
            for capability in items
        ]

    def capabilities(self) -> list[FeatureCapability]:
        database = self.flow.database_status()
        warehouse = self.flow.lab_warehouse_status()
        cloud = self.ecs_dispatch_planner.status()
        live = {
            "github": self.flow.github_status(),
            "forge": self.flow.forge_status(),
            "deployment": self.flow.deployer.status(),
            "execution": self.flow.execution_provider.status(),
            "callback_auth": self.flow.callback_auth_status(),
        }
        lab_summary = self.flow.lab_summary()
        leaderboard = self.flow.lab_leaderboard(limit=5)
        harnesses = self.flow.harness_status()
        models = self.flow.model_agent_status()
        openai_runtime = next(
            (
                model.get("runtime", {})
                for model in models["models"]
                if model.get("provider") == "openai"
            ),
            {"configured": False, "missing": ["OPENAI_API_KEY"]},
        )
        event_status = self._event_ingest_status()
        capabilities = [
            self._cap(
                "request-intake",
                "bounded user request intake",
                "control-plane",
                "reject bad or oversized work before dispatch",
                "ready",
                [
                    "/jobs",
                    "RequestValidator",
                    "tests:test_rejects_oversized_prompt_before_dispatch",
                ],
                critical=True,
            ),
            self._cap(
                "prompt-upgrade",
                "prompt upgrade and acceptance criteria",
                "agent-contract",
                "turn vague work into a concise repo-edit brief",
                "ready",
                ["PromptUpgrader", "job_event:prompt_upgraded"],
                critical=True,
            ),
            self._cap(
                "worker-payload",
                "container-ready worker payload",
                "cloud-execution",
                "ship exact job/model/agent/harness contract into cloud workers",
                "ready",
                ["/jobs/{id}/worker-payload", "WorkerJobPayload"],
                critical=True,
            ),
            self._cap(
                "durable-queue",
                "durable queue and worker leases",
                "cloud-execution",
                "recover active work from lease heartbeat and expiry state",
                "ready",
                ["/jobs/leases", "/jobs/leases/recover-stale"],
                critical=True,
            ),
            self._cap(
                "ecs-submit",
                "AWS ECS/Fargate live submit",
                "cloud-execution",
                "launch one isolated worker container per job",
                "ready" if cloud["configured"] and cloud["submit_enabled"] else "env_gated",
                ["/jobs/{id}/cloud-dispatch-plan", "/jobs/{id}/cloud-dispatch"],
                cloud.get("missing", []),
                ["configure ECS env", "set AGENT_CLOUD_ECS_SUBMIT_ENABLED=1"],
                critical=True,
            ),
            self._cap(
                "signed-worker-callbacks",
                "signed worker callbacks",
                "cloud-execution",
                "accept cloud worker progress without trusting public unauthenticated posts",
                "ready" if live["callback_auth"]["configured"] else "env_gated",
                ["/integrations/callback-auth/status", "callback_auth.py"],
                live["callback_auth"].get("missing", []),
                ["set AGENT_CLOUD_WORKER_CALLBACK_SECRET"],
                critical=True,
            ),
            self._cap(
                "artifact-storage",
                "artifact references and optional S3 storage",
                "evidence",
                "make transcripts/diffs/run artifacts durable and hash-addressable",
                "ready"
                if self.flow.artifact_storage.provider == "local"
                else "env_gated",
                ["/jobs/{id}/artifacts", "artifact_store.py"],
                [],
                ["configure S3 provider for production durability"],
                critical=True,
            ),
            self._cap(
                "provenance",
                "run provenance manifest",
                "evidence",
                "tie final decisions to hashed source, artifact, and deployment evidence",
                "ready",
                ["/jobs/{id}/provenance", "provenance.py"],
                critical=True,
            ),
            self._cap(
                "policy-gates",
                "promotion and deploy gates",
                "safety",
                "stop sync/deploy when tests, artifacts, paths, or secrets fail",
                "ready",
                ["policy_gate_result", "promotion-evaluation.v1"],
                critical=True,
            ),
            self._cap(
                "analysis-lab",
                "analysis cases and experiment reports",
                "language-model-lab",
                "compare model/agent/harness tuples by case family and failure mode",
                "ready",
                ["/analysis/cases", "/analysis/experiments/{id}/report"],
                critical=True,
            ),
            self._cap(
                "slm-dataset-export",
                "redacted SLM dataset export",
                "language-model-lab",
                "turn replay artifacts into deterministic train/eval/holdout JSONL",
                "ready",
                ["/datasets/exports", "scripts/export_slm_dataset.py"],
                critical=True,
            ),
            self._cap(
                "lab-router",
                "leaderboard-backed lab router",
                "language-model-lab",
                "route new work from measured evidence instead of static defaults",
                "ready" if leaderboard else "local_ready",
                ["/lab/router/recommend", "/lab/leaderboard"],
                [],
                ["seed leaderboard history with task-suite runs"],
            ),
            self._cap(
                "lab-warehouse",
                "DuckDB lab warehouse read model",
                "analytics",
                "separate lab analytics from operational writes",
                "ready" if warehouse["configured"] else "local_ready",
                ["/lab/warehouse/status", "/lab/warehouse/refresh"],
                warehouse.get("missing", []),
                ["install duckdb in local Python env if materialization is needed"],
            ),
            self._cap(
                "postgres-operational",
                "optional Postgres operational adapter",
                "storage",
                "move operational locks and writes to production database infrastructure",
                "ready" if database["production_target"]["configured"] else "env_gated",
                ["/integrations/database/status", "database.py"],
                database["production_target"].get("missing", []),
                ["set AGENT_CLOUD_DB_PROVIDER=postgres", "set AGENT_CLOUD_POSTGRES_DSN"],
                critical=True,
            ),
            self._cap(
                "forge-agnostic",
                "git-agnostic review target layer",
                "git-forge",
                "avoid locking the product to GitHub-only repo review flows",
                "ready",
                ["/integrations/forge/status", "forge.py"],
            ),
            self._cap(
                "github-app",
                "GitHub App repo sync",
                "git-forge",
                "use installation-scoped credentials for real GitHub PRs",
                "ready" if live["github"].configured else "env_gated",
                ["/integrations/github/status"],
                live["github"].missing,
                ["configure GitHub App id, installation id, and private key"],
            ),
            self._cap(
                "gitlab-bitbucket-gitea",
                "provider-native non-GitHub review adapters",
                "git-forge",
                "create native merge/pull requests outside GitHub",
                "contract",
                ["/integrations/forge/status"],
                [
                    key
                    for key, value in live["forge"].items()
                    if key in {"gitlab", "bitbucket", "gitea"} and not value["configured"]
                ],
                ["implement provider-native API adapters after credentials are selected"],
            ),
            self._cap(
                "openai-model-path",
                "OpenAI Responses model and edit adapter",
                "model-runtime",
                "exercise a real external model path behind explicit gates",
                "ready" if openai_runtime["configured"] else "env_gated",
                ["/models", "harness_adapters.py"],
                openai_runtime.get("missing", []),
                ["set OPENAI_API_KEY and explicit enable flags"],
            ),
            self._cap(
                "harness-index",
                "curated harness registry and custom harness contracts",
                "agent-harness",
                "dispatch across local, managed, CLI, SDK, and custom agent harnesses",
                "ready" if len(harnesses["top_20"]) == 20 else "partial",
                ["/harnesses", "/harnesses/{id}"],
            ),
            self._cap(
                "external-harness-adapters",
                "verified external harness execution adapters",
                "agent-harness",
                "execute known third-party harnesses without generic shell ambiguity",
                "partial",
                ["pi-coding-agent adapter", "openai-codex-cli adapter"],
                ["other indexed harnesses are dispatch contracts"],
                ["add adapters one at a time with security profiles and replay tests"],
            ),
            self._cap(
                "event-intake",
                "signed idempotent event intake",
                "automation",
                "let GitHub/GitLab/CI/webhook events trigger bounded agent jobs safely",
                "ready" if event_status["configured"] else "local_ready",
                ["/events/intake", "/events/intakes", "/integrations/events/status"],
                event_status["missing"],
                ["set AGENT_CLOUD_EVENT_INGEST_SECRET for public webhook use"],
                critical=True,
            ),
            self._cap(
                "idempotency",
                "event idempotency and dedupe",
                "automation",
                "avoid duplicate jobs when webhook providers retry",
                "ready",
                ["event_intakes.idempotency_key", "/events/intakes"],
                critical=True,
            ),
            self._cap(
                "operator-doctor",
                "operator doctor and readiness scorecard",
                "operations",
                "make environment gaps and live cutover blockers obvious",
                "ready",
                ["/readiness/scorecard", "scripts/doctor.py"],
                critical=True,
            ),
            self._cap(
                "api-auth-quota",
                "API keys and per-user quota guard",
                "multi-tenant",
                "protect shared capacity before dispatch",
                "ready",
                ["/auth/status", "/users/{id}/quota"],
                [],
                ["replace static API keys with tenant-scoped identity provider for production"],
            ),
            self._cap(
                "tenant-isolation",
                "tenant-scoped workspaces and policies",
                "multi-tenant",
                "separate user data, quotas, credentials, and audit trails",
                "partial",
                ["user_id on jobs", "quota ledger"],
                ["no full tenant RBAC or per-tenant secrets yet"],
                ["add tenant table and scoped credentials when production identity is chosen"],
                critical=True,
            ),
            self._cap(
                "observability",
                "metrics, traces, and alert hooks",
                "operations",
                "debug latency, spend, queue health, and worker failures without log scraping",
                "partial",
                ["events stream", "budget ledger", "worker callbacks"],
                ["no Prometheus/OpenTelemetry exporter yet"],
                ["add metrics endpoint and trace ids across worker callbacks"],
            ),
            self._cap(
                "drift-regression",
                "continuous regression and benchmark gates",
                "quality",
                "prevent model/router/harness changes from silently degrading",
                "local_ready",
                ["scripts/evaluate_mvp.py", "scripts/evaluate_task_suite.py"],
                [],
                ["wire these into CI once repository checks are configured"],
                critical=True,
            ),
            self._cap(
                "local-lab-appliance",
                "one-command lab-in-a-box demo",
                "product-demo",
                "prove the full lab loop without external services",
                "ready",
                ["scripts/demo_lab_in_a_box.py", "/lab/appliance/status"],
            ),
        ]
        if lab_summary.get("total_runs", 0) == 0:
            capabilities.append(
                self._cap(
                    "seeded-run-history",
                    "seeded lab evidence",
                    "language-model-lab",
                    "show router and leaderboard behavior with measured runs",
                    "local_ready",
                    ["scripts/evaluate_task_suite.py"],
                    [],
                    ["run task-suite before demos that need warm leaderboard evidence"],
                )
            )
        return capabilities

    @staticmethod
    def _cap(
        capability_id: str,
        title: str,
        category: str,
        target: str,
        status: str,
        evidence: list[str],
        blockers: list[str] | None = None,
        next_steps: list[str] | None = None,
        critical: bool = False,
    ) -> FeatureCapability:
        return FeatureCapability(
            capability_id=capability_id,
            title=title,
            category=category,
            target=target,
            status=status,
            evidence=evidence,
            blockers=blockers or [],
            next_steps=next_steps or [],
            critical=critical,
        )

    @staticmethod
    def _status_score(status: str) -> float:
        return {
            "ready": 1.0,
            "local_ready": 0.85,
            "env_gated": 0.65,
            "partial": 0.45,
            "contract": 0.3,
            "missing": 0.0,
        }.get(status, 0.0)

    @staticmethod
    def _weight(capability: FeatureCapability) -> float:
        return 2.0 if capability.critical else 1.0

    @staticmethod
    def _event_ingest_status() -> dict[str, Any]:
        import os

        configured = bool(os.environ.get("AGENT_CLOUD_EVENT_INGEST_SECRET", "").strip())
        return {
            "configured": configured,
            "mode": "signed-hmac" if configured else "unsigned-local",
            "missing": [] if configured else ["AGENT_CLOUD_EVENT_INGEST_SECRET"],
        }
