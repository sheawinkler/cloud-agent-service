from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cloud_agent_service.database import DatabaseStatus, connect_database, database_status
from cloud_agent_service.models import JobStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, db_path: str | Path, provider: str | None = None) -> None:
        self.db_path = Path(db_path)
        self.provider = provider or os.environ.get("AGENT_CLOUD_DB_PROVIDER", "sqlite")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @classmethod
    def from_env(cls, runtime_root: str | Path) -> JobStore:
        root = Path(runtime_root)
        provider = os.environ.get("AGENT_CLOUD_DB_PROVIDER", "sqlite")
        default_name = "jobs.duckdb" if provider.strip().lower() == "duckdb" else "jobs.sqlite3"
        return cls(
            os.environ.get("AGENT_CLOUD_DB", str(root / default_name)),
            provider=provider,
        )

    def status(self) -> DatabaseStatus:
        return database_status(self.db_path, self.provider)

    def _connect(self):
        return connect_database(self.db_path, self.provider)

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        normalized_prompt TEXT NOT NULL DEFAULT '',
                        repo_path TEXT NOT NULL,
                        repo_provider TEXT NOT NULL DEFAULT 'local',
                        git_url TEXT,
                        github_repo TEXT,
                        parent_job_id TEXT,
                        model_id TEXT NOT NULL DEFAULT 'local-deterministic',
                        agent_id TEXT NOT NULL DEFAULT 'repo-editor-v1',
                        harness_id TEXT NOT NULL DEFAULT 'local-template',
                        routing_policy TEXT NOT NULL DEFAULT 'fixed',
                        routing_decision_json TEXT NOT NULL DEFAULT '{}',
                        working_branch TEXT NOT NULL DEFAULT '',
                        workspace_path TEXT NOT NULL DEFAULT '',
                        base_branch TEXT NOT NULL,
                        deploy_policy TEXT NOT NULL,
                        token_budget INTEGER NOT NULL,
                        max_prompt_chars INTEGER NOT NULL DEFAULT 8000,
                        max_runtime_seconds INTEGER NOT NULL DEFAULT 600,
                        max_changed_files INTEGER NOT NULL DEFAULT 12,
                        status TEXT NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS job_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS budget_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        token_delta INTEGER NOT NULL,
                        runtime_seconds REAL NOT NULL DEFAULT 0,
                        note TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS repo_memory (
                        repo_key TEXT PRIMARY KEY,
                        provider TEXT NOT NULL,
                        profile_json TEXT NOT NULL,
                        test_commands_json TEXT NOT NULL,
                        last_job_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS lab_runs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        repo_provider TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        harness_id TEXT NOT NULL DEFAULT 'local-template',
                        job_status TEXT NOT NULL,
                        promotion_status TEXT NOT NULL,
                        promotion_reason TEXT NOT NULL,
                        deployment_status TEXT NOT NULL,
                        changed_files_count INTEGER NOT NULL,
                        tests_failed_count INTEGER NOT NULL,
                        token_budget INTEGER NOT NULL,
                        tokens_used INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analysis_cases (
                        case_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        category TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        task_ids_json TEXT NOT NULL,
                        model_ids_json TEXT NOT NULL,
                        agent_ids_json TEXT NOT NULL,
                        harness_ids_json TEXT NOT NULL,
                        success_criteria_json TEXT NOT NULL,
                        tags_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analysis_experiments (
                        experiment_id TEXT PRIMARY KEY,
                        case_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        spec_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(case_id) REFERENCES analysis_cases(case_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analysis_experiment_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        experiment_id TEXT NOT NULL,
                        case_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        analysis_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(experiment_id) REFERENCES analysis_experiments(experiment_id),
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS dataset_exports (
                        export_id TEXT PRIMARY KEY,
                        artifact_path TEXT NOT NULL,
                        split_paths_json TEXT NOT NULL,
                        counts_json TEXT NOT NULL,
                        source_job_ids_json TEXT NOT NULL,
                        lineage_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_dispatches (
                        dispatch_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        status TEXT NOT NULL,
                        task_arn TEXT,
                        region TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        response_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_callbacks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        callback_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_refs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        artifact_type TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        uri TEXT NOT NULL,
                        path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        bytes INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS analysis_experiment_batches (
                        batch_id TEXT PRIMARY KEY,
                        experiment_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        max_concurrency INTEGER NOT NULL,
                        requested_jobs INTEGER NOT NULL,
                        completed_jobs INTEGER NOT NULL,
                        failed_jobs INTEGER NOT NULL,
                        job_ids_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(experiment_id) REFERENCES analysis_experiments(experiment_id)
                    )
                    """
                )
                self._ensure_job_columns(conn)
                self._ensure_lab_run_columns(conn)
                self._ensure_dataset_export_columns(conn)
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lab_runs_model_agent_status
                    ON lab_runs (model_id, agent_id, promotion_status)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lab_runs_model_agent_harness_status
                    ON lab_runs (model_id, agent_id, harness_id, promotion_status)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lab_runs_harness_status
                    ON lab_runs (harness_id, promotion_status)
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_lab_runs_updated_at
                    ON lab_runs (updated_at)
                    """
                )

    def _ensure_job_columns(self, conn: Any) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        additions = {
            "max_prompt_chars": "INTEGER NOT NULL DEFAULT 8000",
            "max_runtime_seconds": "INTEGER NOT NULL DEFAULT 600",
            "max_changed_files": "INTEGER NOT NULL DEFAULT 12",
            "repo_provider": "TEXT NOT NULL DEFAULT 'local'",
            "git_url": "TEXT",
            "github_repo": "TEXT",
            "parent_job_id": "TEXT",
            "model_id": "TEXT NOT NULL DEFAULT 'local-deterministic'",
            "agent_id": "TEXT NOT NULL DEFAULT 'repo-editor-v1'",
            "harness_id": "TEXT NOT NULL DEFAULT 'local-template'",
            "routing_policy": "TEXT NOT NULL DEFAULT 'fixed'",
            "routing_decision_json": "TEXT NOT NULL DEFAULT '{}'",
            "working_branch": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

    def _ensure_lab_run_columns(self, conn: Any) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(lab_runs)").fetchall()
        }
        additions = {
            "harness_id": "TEXT NOT NULL DEFAULT 'local-template'",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE lab_runs ADD COLUMN {name} {definition}")

    def _ensure_dataset_export_columns(self, conn: Any) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(dataset_exports)").fetchall()
        }
        additions = {
            "lineage_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE dataset_exports ADD COLUMN {name} {definition}")

    def create_job(self, job: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, user_id, prompt, repo_path, repo_provider,
                        git_url, github_repo, parent_job_id, working_branch,
                        model_id, agent_id, harness_id, routing_policy,
                        routing_decision_json, base_branch, deploy_policy, token_budget,
                        max_prompt_chars, max_runtime_seconds, max_changed_files,
                        status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["job_id"],
                        job["user_id"],
                        job["prompt"],
                        job["repo_path"],
                        job["repo_provider"],
                        job.get("git_url"),
                        job.get("github_repo"),
                        job.get("parent_job_id"),
                        job["working_branch"],
                        job["model_id"],
                        job["agent_id"],
                        job["harness_id"],
                        job["routing_policy"],
                        json.dumps(job["routing_decision_json"], sort_keys=True),
                        job["base_branch"],
                        job["deploy_policy"],
                        job["token_budget"],
                        job["max_prompt_chars"],
                        job["max_runtime_seconds"],
                        job["max_changed_files"],
                        JobStatus.CREATED.value,
                        now,
                        now,
                    ),
                )

    def upsert_repo_memory(
        self,
        repo_key: str,
        provider: str,
        profile: dict[str, Any],
        test_commands: list[str],
        job_id: str,
    ) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO repo_memory (
                        repo_key, provider, profile_json, test_commands_json,
                        last_job_id, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo_key) DO UPDATE SET
                        provider = excluded.provider,
                        profile_json = excluded.profile_json,
                        test_commands_json = excluded.test_commands_json,
                        last_job_id = excluded.last_job_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        repo_key,
                        provider,
                        json.dumps(profile, sort_keys=True),
                        json.dumps(test_commands, sort_keys=True),
                        job_id,
                        utc_now(),
                    ),
                )

    def get_repo_memory(self, repo_key: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM repo_memory WHERE repo_key = ?",
                (repo_key,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["profile_json"] = json.loads(data["profile_json"])
        data["test_commands_json"] = json.loads(data["test_commands_json"])
        return data

    def upsert_analysis_case(self, case: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO analysis_cases (
                        case_id, title, category, prompt, task_ids_json, model_ids_json,
                        agent_ids_json, harness_ids_json, success_criteria_json, tags_json,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(case_id) DO UPDATE SET
                        title = excluded.title,
                        category = excluded.category,
                        prompt = excluded.prompt,
                        task_ids_json = excluded.task_ids_json,
                        model_ids_json = excluded.model_ids_json,
                        agent_ids_json = excluded.agent_ids_json,
                        harness_ids_json = excluded.harness_ids_json,
                        success_criteria_json = excluded.success_criteria_json,
                        tags_json = excluded.tags_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        case["case_id"],
                        case["title"],
                        case["category"],
                        case["prompt"],
                        json.dumps(case["task_ids"], sort_keys=True),
                        json.dumps(case["model_ids"], sort_keys=True),
                        json.dumps(case["agent_ids"], sort_keys=True),
                        json.dumps(case["harness_ids"], sort_keys=True),
                        json.dumps(case["success_criteria"], sort_keys=True),
                        json.dumps(case["tags"], sort_keys=True),
                        now,
                        now,
                    ),
                )

    def list_analysis_cases(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM analysis_cases
                ORDER BY category, case_id
                """
            ).fetchall()
        return [self._analysis_case_row(row) for row in rows]

    def get_analysis_case(self, case_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM analysis_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
        return self._analysis_case_row(row) if row else None

    def create_analysis_experiment(self, spec: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO analysis_experiments (
                        experiment_id, case_id, name, spec_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(experiment_id) DO UPDATE SET
                        case_id = excluded.case_id,
                        name = excluded.name,
                        spec_json = excluded.spec_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        spec["experiment_id"],
                        spec["case_id"],
                        spec["name"],
                        json.dumps(spec, sort_keys=True),
                        now,
                        now,
                    ),
                )

    def get_analysis_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM analysis_experiments WHERE experiment_id = ?
                """,
                (experiment_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["spec_json"] = json.loads(data["spec_json"])
        return data

    def add_analysis_experiment_run(
        self,
        experiment_id: str,
        case_id: str,
        job_id: str,
        analysis: dict[str, Any],
    ) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO analysis_experiment_runs (
                        experiment_id, case_id, job_id, analysis_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        experiment_id,
                        case_id,
                        job_id,
                        json.dumps(analysis, sort_keys=True),
                        utc_now(),
                    ),
                )

    def list_analysis_experiment_runs(self, experiment_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM analysis_experiment_runs
                WHERE experiment_id = ?
                ORDER BY id
                """,
                (experiment_id,),
            ).fetchall()
        data = []
        for row in rows:
            item = dict(row)
            item["analysis_json"] = json.loads(item["analysis_json"])
            data.append(item)
        return data

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [self._encode(value) for value in fields.values()]
        values.append(job_id)
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(f"UPDATE jobs SET {assignments} WHERE job_id = ?", values)

    def add_event(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO job_events (job_id, event_type, payload_json, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, event_type, json.dumps(payload or {}, sort_keys=True), utc_now()),
                )

    def add_budget_entry(
        self,
        job_id: str,
        stage: str,
        token_delta: int,
        runtime_seconds: float = 0,
        note: str = "",
    ) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO budget_ledger (
                        job_id, stage, token_delta, runtime_seconds, note, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (job_id, stage, token_delta, runtime_seconds, note, utc_now()),
                )

    def upsert_lab_run(self, run: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO lab_runs (
                        job_id, user_id, repo_provider, model_id, agent_id, harness_id,
                        job_status, promotion_status, promotion_reason,
                        deployment_status, changed_files_count, tests_failed_count,
                        token_budget, tokens_used, created_at, updated_at
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

    def upsert_dataset_export(self, export: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO dataset_exports (
                        export_id, artifact_path, split_paths_json, counts_json,
                        source_job_ids_json, lineage_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(export_id) DO UPDATE SET
                        artifact_path = excluded.artifact_path,
                        split_paths_json = excluded.split_paths_json,
                        counts_json = excluded.counts_json,
                        source_job_ids_json = excluded.source_job_ids_json,
                        lineage_json = excluded.lineage_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        export["export_id"],
                        export["artifact_path"],
                        json.dumps(export["split_paths"], sort_keys=True),
                        json.dumps(export["counts"], sort_keys=True),
                        json.dumps(export["source_job_ids"], sort_keys=True),
                        json.dumps(export.get("lineage", {}), sort_keys=True),
                        now,
                        now,
                    ),
                )

    def get_dataset_export(self, export_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM dataset_exports WHERE export_id = ?",
                (export_id,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["split_paths"] = json.loads(data.pop("split_paths_json"))
        data["counts"] = json.loads(data.pop("counts_json"))
        data["source_job_ids"] = json.loads(data.pop("source_job_ids_json"))
        data["lineage"] = json.loads(data.pop("lineage_json"))
        return data

    def upsert_cloud_dispatch(self, dispatch: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO cloud_dispatches (
                        dispatch_id, job_id, provider, mode, status, task_arn, region,
                        request_json, response_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dispatch_id) DO UPDATE SET
                        status = excluded.status,
                        task_arn = excluded.task_arn,
                        response_json = excluded.response_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        dispatch["dispatch_id"],
                        dispatch["job_id"],
                        dispatch["provider"],
                        dispatch["mode"],
                        dispatch["status"],
                        dispatch.get("task_arn"),
                        dispatch["region"],
                        json.dumps(dispatch["request"], sort_keys=True),
                        json.dumps(dispatch["response"], sort_keys=True),
                        now,
                        now,
                    ),
                )

    def get_cloud_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM cloud_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        return self._cloud_dispatch_row(row) if row else None

    def list_cloud_dispatches(self, job_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM cloud_dispatches"
        params: list[Any] = []
        if job_id:
            query += " WHERE job_id = ?"
            params.append(job_id)
        query += " ORDER BY created_at DESC"
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._cloud_dispatch_row(row) for row in rows]

    def add_worker_callback(
        self,
        job_id: str,
        callback_type: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO worker_callbacks (
                        job_id, callback_type, status, payload_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        callback_type,
                        status,
                        json.dumps(payload, sort_keys=True),
                        utc_now(),
                    ),
                )

    def list_worker_callbacks(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM worker_callbacks
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [self._worker_callback_row(row) for row in rows]

    def add_artifact_refs(self, refs: list[dict[str, Any]]) -> None:
        if not refs:
            return
        with closing(self._connect()) as conn:
            with conn:
                for ref in refs:
                    conn.execute(
                        """
                        INSERT INTO artifact_refs (
                            job_id, artifact_type, provider, uri, path, sha256, bytes, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ref["job_id"],
                            ref["artifact_type"],
                            ref["provider"],
                            ref["uri"],
                            ref["path"],
                            ref["sha256"],
                            ref["bytes"],
                            utc_now(),
                        ),
                    )

    def list_artifact_refs(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM artifact_refs
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_analysis_experiment_batch(self, batch: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO analysis_experiment_batches (
                        batch_id, experiment_id, status, max_concurrency, requested_jobs,
                        completed_jobs, failed_jobs, job_ids_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(batch_id) DO UPDATE SET
                        status = excluded.status,
                        completed_jobs = excluded.completed_jobs,
                        failed_jobs = excluded.failed_jobs,
                        job_ids_json = excluded.job_ids_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        batch["batch_id"],
                        batch["experiment_id"],
                        batch["status"],
                        batch["max_concurrency"],
                        batch["requested_jobs"],
                        batch["completed_jobs"],
                        batch["failed_jobs"],
                        json.dumps(batch["job_ids"], sort_keys=True),
                        now,
                        now,
                    ),
                )

    def get_analysis_experiment_batch(self, batch_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM analysis_experiment_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
        return self._analysis_batch_row(row) if row else None

    def list_analysis_experiment_batches(self, experiment_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM analysis_experiment_batches
                WHERE experiment_id = ?
                ORDER BY created_at DESC
                """,
                (experiment_id,),
            ).fetchall()
        return [self._analysis_batch_row(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def get_lab_run(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM lab_runs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_lab_runs(
        self,
        limit: int = 50,
        model_id: str | None = None,
        agent_id: str | None = None,
        harness_id: str | None = None,
        promotion_status: str | None = None,
    ) -> list[dict[str, Any]]:
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

    def lab_summary(self) -> dict[str, Any]:
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

    def lab_leaderboard(self, limit: int = 50) -> list[dict[str, Any]]:
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
        leaderboard: list[dict[str, Any]] = []
        for row in rows:
            total_runs = int(row["total_runs"])
            promote_count = int(row["promote_count"])
            leaderboard.append(
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
        return leaderboard

    def user_usage(self, user_id: str) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            jobs = conn.execute(
                """
                SELECT
                    COUNT(*) AS jobs_count,
                    COALESCE(SUM(token_budget), 0) AS token_budget_reserved
                FROM jobs
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            active = conn.execute(
                """
                SELECT COUNT(*) AS active_jobs_count
                FROM jobs
                WHERE user_id = ?
                  AND status NOT IN ('succeeded', 'failed', 'cancelled')
                """,
                (user_id,),
            ).fetchone()
            tokens_used = conn.execute(
                """
                SELECT COALESCE(SUM(budget_ledger.token_delta), 0) AS tokens_used
                FROM budget_ledger
                JOIN jobs ON jobs.job_id = budget_ledger.job_id
                WHERE jobs.user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return {
            "user_id": user_id,
            "jobs_count": int(jobs["jobs_count"]),
            "active_jobs_count": int(active["active_jobs_count"]),
            "token_budget_reserved": int(jobs["token_budget_reserved"]),
            "tokens_used": int(tokens_used["tokens_used"]),
        }

    def list_jobs(self, limit: int = 50, user_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if user_id:
            query += " WHERE user_id = ?"
            params.append(user_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_budget_entries(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, stage, token_delta, runtime_seconds, note, created_at
                FROM budget_ledger
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def budget_tokens_used(self, job_id: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(token_delta), 0) AS tokens_used
                FROM budget_ledger
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return int(row["tokens_used"])

    def claim_next_queued_job(self) -> str | None:
        with closing(self._connect()) as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """
                    SELECT job_id
                    FROM jobs
                    WHERE status = ?
                    ORDER BY created_at
                    LIMIT 1
                    """,
                    (JobStatus.QUEUED.value,),
                ).fetchone()
                if not row:
                    conn.execute("COMMIT")
                    return None

                job_id = row["job_id"]
                cursor = conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, updated_at = ?
                    WHERE job_id = ? AND status = ?
                    """,
                    (
                        JobStatus.DISPATCHED.value,
                        utc_now(),
                        job_id,
                        JobStatus.QUEUED.value,
                    ),
                )
                conn.execute("COMMIT")
                return job_id if cursor.rowcount == 1 else None
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_events(self, job_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, payload_json, created_at
                FROM job_events
                WHERE job_id = ?
                ORDER BY id
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_events_after(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, event_type, payload_json, created_at
                FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id
                """,
                (job_id, after_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _encode(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)
        if isinstance(value, JobStatus):
            return value.value
        return value

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["result_json"] = json.loads(data["result_json"])
        data["routing_decision_json"] = json.loads(data["routing_decision_json"])
        return data

    @staticmethod
    def _analysis_case_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["task_ids"] = json.loads(data.pop("task_ids_json"))
        data["model_ids"] = json.loads(data.pop("model_ids_json"))
        data["agent_ids"] = json.loads(data.pop("agent_ids_json"))
        data["harness_ids"] = json.loads(data.pop("harness_ids_json"))
        data["success_criteria"] = json.loads(data.pop("success_criteria_json"))
        data["tags"] = json.loads(data.pop("tags_json"))
        return data

    @staticmethod
    def _cloud_dispatch_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["request"] = json.loads(data.pop("request_json"))
        data["response"] = json.loads(data.pop("response_json"))
        return data

    @staticmethod
    def _worker_callback_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return data

    @staticmethod
    def _analysis_batch_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["job_ids"] = json.loads(data.pop("job_ids_json"))
        return data
