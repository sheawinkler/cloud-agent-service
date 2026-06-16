from __future__ import annotations

import importlib
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AUTOINCREMENT_TABLES = {
    "job_events": "job_events_id_seq",
    "budget_ledger": "budget_ledger_id_seq",
    "analysis_experiment_runs": "analysis_experiment_runs_id_seq",
    "worker_callbacks": "worker_callbacks_id_seq",
    "artifact_refs": "artifact_refs_id_seq",
}


@dataclass(frozen=True)
class DatabaseStatus:
    provider: str
    path: str
    configured: bool
    mode: str
    missing: list[str]
    notes: list[str]


def database_status(db_path: str | Path, provider: str | None = None) -> DatabaseStatus:
    selected = normalize_provider(provider or os.environ.get("AGENT_CLOUD_DB_PROVIDER", "sqlite"))
    missing: list[str] = []
    notes: list[str] = []
    configured = True
    mode = "embedded-operational"
    if selected == "duckdb":
        try:
            importlib.import_module("duckdb")
        except ModuleNotFoundError:
            configured = False
            missing.append("duckdb")
        mode = "embedded-lab-analytics"
        notes.append(
            "DuckDB is opt-in and best suited for local lab analytics, not multi-writer queues."
        )
    if selected == "postgres":
        mode = "production-operational-adapter"
        if not os.environ.get("AGENT_CLOUD_POSTGRES_DSN"):
            configured = False
            missing.append("AGENT_CLOUD_POSTGRES_DSN")
        try:
            importlib.import_module("psycopg")
        except ModuleNotFoundError:
            configured = False
            missing.append("psycopg")
        notes.append(
            "Postgres is the production operational-store target. SQLite remains "
            "the default local write path; use AGENT_CLOUD_DB_PROVIDER=postgres "
            "only with a configured DSN and psycopg runtime."
        )
    return DatabaseStatus(
        provider=selected,
        path=str(db_path),
        configured=configured,
        mode=mode,
        missing=missing,
        notes=notes,
    )


def normalize_provider(provider: str) -> str:
    selected = provider.strip().lower().replace("-", "_")
    if selected in {"sqlite", "sqlite3", ""}:
        return "sqlite"
    if selected == "duckdb":
        return "duckdb"
    if selected in {"postgres", "postgresql", "rds_postgres"}:
        return "postgres"
    raise ValueError(f"unsupported database provider: {provider}")


def connect_database(db_path: str | Path, provider: str):
    selected = normalize_provider(provider)
    if selected == "sqlite":
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn
    if selected == "postgres":
        return PostgresConnection(str(db_path))
    return DuckDbConnection(db_path)


def production_database_status() -> DatabaseStatus:
    provider = os.environ.get("AGENT_CLOUD_PRODUCTION_DB_PROVIDER", "postgres")
    db_path = os.environ.get("AGENT_CLOUD_POSTGRES_DSN", "postgres://<not-configured>")
    return database_status(db_path, provider)


class DuckDbRow(dict[str, Any]):
    pass


class DuckDbCursor:
    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor
        self.rowcount = getattr(cursor, "rowcount", -1)
        self._columns = [column[0] for column in (cursor.description or [])]

    def fetchone(self) -> DuckDbRow | None:
        row = self.cursor.fetchone()
        if row is None:
            return None
        return DuckDbRow(zip(self._columns, row, strict=False))

    def fetchall(self) -> list[DuckDbRow]:
        return [DuckDbRow(zip(self._columns, row, strict=False)) for row in self.cursor.fetchall()]


class DuckDbConnection:
    def __init__(self, db_path: str | Path) -> None:
        duckdb = importlib.import_module("duckdb")
        self.db_path = Path(db_path)
        self.conn = duckdb.connect(str(self.db_path))
        self.isolation_level = None
        self._init_sequences()

    def _init_sequences(self) -> None:
        for sequence_name in AUTOINCREMENT_TABLES.values():
            self.conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {sequence_name} START 1")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> DuckDbConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def commit(self) -> None:
        # DuckDB autocommits unless an explicit transaction is open.
        try:
            self.conn.commit()
        except Exception:
            pass

    def rollback(self) -> None:
        try:
            self.conn.rollback()
        except Exception:
            pass

    def execute(self, sql: str, params: Any | None = None) -> DuckDbCursor:
        return DuckDbCursor(self.conn.execute(self._normalize_sql(sql), params or ()))

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        normalized = sql
        for table, sequence_name in AUTOINCREMENT_TABLES.items():
            table_marker = f"CREATE TABLE IF NOT EXISTS {table}"
            if table_marker not in normalized:
                continue
            normalized = normalized.replace(
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                f"id BIGINT PRIMARY KEY DEFAULT nextval('{sequence_name}')",
            )
            break
        stripped = " ".join(normalized.strip().split()).upper()
        if stripped == "BEGIN IMMEDIATE":
            return "BEGIN TRANSACTION"
        return normalized


class StaticCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self.rowcount = len(self._rows)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class PostgresConnection:
    PRAGMA_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\(([^)]+)\)\s*$", re.I)

    def __init__(self, dsn: str) -> None:
        psycopg = importlib.import_module("psycopg")
        rows = importlib.import_module("psycopg.rows")
        self.dsn = dsn or os.environ.get("AGENT_CLOUD_POSTGRES_DSN", "")
        if not self.dsn:
            raise RuntimeError("AGENT_CLOUD_POSTGRES_DSN is required for postgres provider")
        self.conn = psycopg.connect(self.dsn, row_factory=rows.dict_row)
        self.isolation_level = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> PostgresConnection:
        self.conn.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.conn.__exit__(exc_type, exc, tb)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def execute(self, sql: str, params: Any | None = None) -> Any:
        table_name = self._pragma_table_name(sql)
        if table_name:
            return StaticCursor(self._table_info(table_name))
        return self.conn.execute(self._normalize_sql(sql), params or ())

    def _table_info(self, table_name: str) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        return list(cursor.fetchall())

    @classmethod
    def _pragma_table_name(cls, sql: str) -> str | None:
        match = cls.PRAGMA_TABLE_INFO_RE.match(sql)
        if not match:
            return None
        return match.group(1).strip().strip('"')

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        normalized = sql.replace(
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "id BIGSERIAL PRIMARY KEY",
        )
        stripped = " ".join(normalized.strip().split()).upper()
        if stripped == "BEGIN IMMEDIATE":
            return "BEGIN"
        return normalized.replace("?", "%s")
