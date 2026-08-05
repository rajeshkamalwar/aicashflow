"""Back up and resumably populate Phase 2B.1 transaction columns."""

import argparse
import hashlib
import os
from pathlib import Path
import sqlite3

from ai_cashflow.config import AppConfig
from ai_cashflow.integrations import AmazonSourceRegistry


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=1)
    args = parser.parse_args()
    database = (args.database or AppConfig().database_path).resolve()
    backup = args.backup.resolve()
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        print(f"backup_sha256={sha256(backup)}")
    key = os.getenv("AI_CASHFLOW_MASTER_KEY", "")
    if not key:
        raise SystemExit("AI_CASHFLOW_MASTER_KEY is required.")
    registry = AmazonSourceRegistry(database, key)
    result = {}
    for _ in range(max(1, args.max_batches)):
        result = registry.migrate_transaction_storage(args.batch_size)
        if result["status"] == "COMPLETE":
            break
    print("migration_status={status} remaining={remaining}".format(**result))
    return 0 if result["status"] == "COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
