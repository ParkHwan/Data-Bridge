"""Vector store — plain pgvector, portable across Postgres / Cloud SQL / AlloyDB."""

from databridge.store.pg import PgVectorStore, SearchHit

__all__ = ["PgVectorStore", "SearchHit"]
