"""Apply the store schema (idempotent) — run as the databridge-migrate Cloud Run job.

Executed by cloudbuild.yaml before every deploy so schema changes (extensions,
indexes, columns) land before the code that needs them (review: Antigravity #1 —
a single pre-deploy task avoids the concurrent-DDL hazard of running
ensure_schema() at service startup across multiple Cloud Run instances).
"""

from __future__ import annotations

import os

from databridge.store import PgVectorStore


def main() -> None:
    store = PgVectorStore(os.environ["DATABRIDGE_DSN"])
    store.ensure_schema()
    print("schema ensured")


if __name__ == "__main__":
    main()
