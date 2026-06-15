from __future__ import annotations

import importlib.util
import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloud_agent_service.database import connect_database


@dataclass(frozen=True)
class LabWarehouseStatus:
    provider: str
    path: str
    configured: bool
    enabled: bool
    mode: str
    missing: list[str]
    notes: list[str]
    materialized_runs: int


class LabWarehouse:
    def __init__(
        self,
        path: str | Path,
        *,
        enabled: bool = True,
        provider: str = "duckdb",
    ) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.provider = provider
        self.configured = importlib.util.find_spec("duckdb") is not None
        if self.enabled and self.configured:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._init_schema()

    @classmethod
    def from_env(cls, runtime_root: str | Path) -> LabWarehouse:
        root = Path(runtime_root)
        enabled = not _falsey(os.environ.get("AGENT_CLOUD_LAB_WAREHOUSE_ENABLED"))
        path = os.environ.get("AGENT_CLOUD_LAB_WAREHOUSE", str(root / "lab.duckdb"))
        return cls(path, enabled=enabled)

    def status(self) -> LabWarehouseStatus:
        missing = [] if self.configured else ["duckdb"]
        mode = "materialized-read-model"
        notes = [
            "DuckDB is the default lab warehouse for leaderboard, experiment, and "
            "dataset analysis; operational job writes remain in JobStore."
        ]
        if not self.enabled:
            mode = "disabled"
            notes.append("Set AGENT_CLOUD_LAB_WAREHOUSE_ENABLED=1 to enable.")
        elif not self.configured:
            mode = "unavailable-fallback"
            notes.append("Falling back to operational store because duckdb is not installed.")
        return LabWarehouseStatus(
            provider=self.provider,
            path=str(self.path),
            configured=self.configured,
            enabled=self.enabled,
            mode=mode,
            missing=missing,
            notes=notes,
            materialized_runs=self._count_runs() if self.ready else 0,
        )

    @property
    def ready(self) -> bool:
        return self.enabled and self.configured

    def _connect(self):
        return connect_database(self.path, self.provider)

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lab_runs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        repo_provider TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        harness_id TEXT NOT NULL,
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

    def refresh_from_store(self, store: Any, limit: int = 5000) -> dict[str, Any]:
        if not self.ready:
            return {"synced": 0, "ready": False}
        runs = store.list_lab_runs(limit=limit)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("DELETE FROM lab_runs")
                for run in reversed(runs):
                    self.upsert_run(run, conn=conn)
        return {"synced": len(runs), "ready": True}

    def upsert_run(self, run: dict[str, Any], conn: Any | None = None) -> None:
        if not self.ready:
            return

        def execute(target: Any) -> None:
            target.execute(
                """
                INSERT INTO lab_runs (
                    job_id, user_id, repo_provider, model_id, agent_id, harness_id,
                    job_status, promotion_status, promotion_reason, deployment_status,
                    changed_files_count, tests_failed_count, token_budget, tokens_used,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    repo_provider = excluded.repo_provider,
                    model_id = excluded.model_id,
                    agent_id = excluded.agent_id,
                    harness_id = excluded.harness_id,
                    job_status = excluded.job_status,
                    promotion_status = excluded.promotion_status,
                    promotion_reason = excluded.promotion_reason,
                    deployment_status = excluded.deployment_status,
                    changed_files_count = excluded.changed_files_count,
                    tests_failed_count = excluded.tests_failed_count,
                    token_budget = excluded.token_budget,
                    tokens_used = excluded.tokens_used,
                    updated_at = excluded.updated_at
                """,
                (
                    run["job_id"],
                    run["user_id"],
                    run["repo_provider"],
                    run["model_id"],
                    run["agent_id"],
                    run["harness_id"],
                    run["job_status"],
                    run["promotion_status"],
                    run["promotion_reason"],
                    run["deployment_status"],
                    run["changed_files_count"],
                    run["tests_failed_count"],
                    run["token_budget"],
                    run["tokens_used"],
                    run["created_at"],
                    run["updated_at"],
                ),
            )

        if conn is not None:
            execute(conn)
            return
        with closing(self._connect()) as owned_conn:
            with owned_conn:
                execute(owned_conn)

    def list_runs(
        self,
        limit: int = 50,
        model_id: str | None = None,
        agent_id: str | None = None,
        harness_id: str | None = None,
        promotion_status: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        limit = max(1, min(limit, 200))
        query = "SELECT * FROM lab_runs"
        filters = []
        params: list[Any] = []
        if model_id:
            filters.append("model_id = ?")
            params.append(model_id)
        if agent_id:
            filters.append("agent_id = ?")
            params.append(agent_id)
        if harness_id:
            filters.append("harness_id = ?")
            params.append(harness_id)
        if promotion_status:
            filters.append("promotion_status = ?")
            params.append(promotion_status)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        if not self.ready:
            return {
                "total_runs": 0,
                "by_promotion_status": {},
                "by_model_agent": [],
                "by_harness": [],
                "by_model_agent_harness": [],
            }
        with closing(self._connect()) as conn:
            total = conn.execute("SELECT COUNT(*) AS total FROM lab_runs").fetchone()["total"]
            by_status_rows = conn.execute(
                """
                SELECT promotion_status, COUNT(*) AS count
                FROM lab_runs
                GROUP BY promotion_status
                ORDER BY promotion_status
                """
            ).fetchall()
            by_model_agent_rows = conn.execute(
                """
                SELECT model_id, agent_id, promotion_status, COUNT(*) AS count
                FROM lab_runs
                GROUP BY model_id, agent_id, promotion_status
                ORDER BY model_id, agent_id, promotion_status
                """
            ).fetchall()
            by_harness_rows = conn.execute(
                """
                SELECT harness_id, promotion_status, COUNT(*) AS count
                FROM lab_runs
                GROUP BY harness_id, promotion_status
                ORDER BY harness_id, promotion_status
                """
            ).fetchall()
            by_model_agent_harness_rows = conn.execute(
                """
                SELECT model_id, agent_id, harness_id, promotion_status, COUNT(*) AS count
                FROM lab_runs
                GROUP BY model_id, agent_id, harness_id, promotion_status
                ORDER BY model_id, agent_id, harness_id, promotion_status
                """
            ).fetchall()
        return {
            "total_runs": int(total),
            "by_promotion_status": {
                row["promotion_status"]: int(row["count"]) for row in by_status_rows
            },
            "by_model_agent": [dict(row) for row in by_model_agent_rows],
            "by_harness": [dict(row) for row in by_harness_rows],
            "by_model_agent_harness": [dict(row) for row in by_model_agent_harness_rows],
        }

    def leaderboard(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        limit = max(1, min(limit, 200))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT
                    model_id,
                    agent_id,
                    harness_id,
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN promotion_status = 'promote' THEN 1 ELSE 0 END)
                        AS promote_count,
                    SUM(CASE WHEN promotion_status = 'needs_review' THEN 1 ELSE 0 END)
                        AS needs_review_count,
                    SUM(CASE WHEN promotion_status = 'reject' THEN 1 ELSE 0 END)
                        AS reject_count,
                    AVG(changed_files_count) AS avg_changed_files,
                    AVG(tests_failed_count) AS avg_tests_failed,
                    AVG(tokens_used) AS avg_tokens_used
                FROM lab_runs
                GROUP BY model_id, agent_id, harness_id
                ORDER BY promote_count DESC, total_runs DESC, model_id, agent_id, harness_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        data: list[dict[str, Any]] = []
        for row in rows:
            total_runs = int(row["total_runs"])
            promote_count = int(row["promote_count"])
            data.append(
                {
                    "model_id": row["model_id"],
                    "agent_id": row["agent_id"],
                    "harness_id": row["harness_id"],
                    "total_runs": total_runs,
                    "promote_count": promote_count,
                    "needs_review_count": int(row["needs_review_count"]),
                    "reject_count": int(row["reject_count"]),
                    "promotion_rate": promote_count / total_runs if total_runs else 0.0,
                    "avg_changed_files": float(row["avg_changed_files"] or 0.0),
                    "avg_tests_failed": float(row["avg_tests_failed"] or 0.0),
                    "avg_tokens_used": float(row["avg_tokens_used"] or 0.0),
                }
            )
        return data

    def _count_runs(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM lab_runs").fetchone()
        return int(row["total"])


def _falsey(value: str | None) -> bool:
    return (value or "").strip().lower() in {"0", "false", "no", "off"}
