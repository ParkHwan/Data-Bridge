"""Confluence ingestion integration."""

from databridge.confluence.batch import (
    BatchResult,
    ConfluenceBatchConfig,
    run_confluence_batch,
)
from databridge.confluence.client import ConfluenceClient

__all__ = ["BatchResult", "ConfluenceBatchConfig", "ConfluenceClient", "run_confluence_batch"]
