from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_agent_service.dataset_export import SlmDatasetExporter  # noqa: E402
from cloud_agent_service.store import JobStore  # noqa: E402


def export_dataset(
    *,
    runtime_root: Path,
    export_id: str | None,
    limit: int,
    promotion_status: str | None,
) -> dict[str, object]:
    store = JobStore.from_env(runtime_root)
    artifacts_dir = Path(os.environ.get("AGENT_CLOUD_ARTIFACTS", str(runtime_root / "artifacts")))
    export = SlmDatasetExporter(store, artifacts_dir).export(
        export_id=export_id,
        limit=limit,
        promotion_status=promotion_status,
    )
    return asdict(export)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export replay artifacts as SLM JSONL splits.")
    parser.add_argument("--runtime-root", default=os.environ.get("AGENT_CLOUD_RUNTIME", ".runtime"))
    parser.add_argument("--export-id", default=None)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--promotion-status", default=None)
    args = parser.parse_args()
    payload = export_dataset(
        runtime_root=Path(args.runtime_root),
        export_id=args.export_id,
        limit=args.limit,
        promotion_status=args.promotion_status,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
