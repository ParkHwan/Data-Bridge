"""Operate generation lifecycle and activation-gating validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from databridge.embed import EmbedderConfigurationError, resolve_embedder
from databridge.store import (
    EmbeddingProfileMismatchError,
    GenerationConcurrencyError,
    GenerationTargetError,
    GenerationValidationError,
    PgVectorStore,
    ProfileModeConfigurationError,
    ValidationQueryConfigurationError,
    resolve_profile_mode,
)
from databridge.store.validation import validate_generation


class DsnConfigurationError(RuntimeError):
    """Raised when the generation CLI has no usable database configuration."""


def resolve_dsn(environ: Mapping[str, str] | None = None) -> str:
    """Resolve the required database DSN without a development fallback."""
    values = os.environ if environ is None else environ
    raw = values.get("DATABRIDGE_DSN")
    if raw is None or not raw.strip():
        raise DsnConfigurationError("DATABRIDGE_DSN is required and must not be empty")
    dsn = raw.strip()
    try:
        conninfo_to_dict(dsn)
    except psycopg.ProgrammingError as exc:
        raise DsnConfigurationError(f"Invalid DATABRIDGE_DSN: {exc}") from exc
    return dsn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Data Bridge embedding generations")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-building")
    create.add_argument("--space", required=True)
    create.add_argument("--discard-inflight", action="store_true")

    for name in ("list", "report"):
        command = commands.add_parser(name)
        command.add_argument("--space", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--space", required=True)
    validate.add_argument("--generation-id", required=True, type=int)
    validate.add_argument("--queries", required=True, type=Path)

    activate = commands.add_parser("activate")
    activate.add_argument("--space", required=True)
    activate.add_argument("--generation-id", required=True, type=int)
    activate.add_argument("--yes", required=True, action="store_true")
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))


def _reference_report(store: PgVectorStore, *, space_key: str) -> dict[str, object]:
    profile = store.profile_report(space_key=space_key)
    return {
        "generations": store.generation_report(space_key=space_key),
        "legacy_null_generation_chunks": profile.null_generation_chunk_count,
        "distinct_profile_count": profile.distinct_profile_count,
        "limitations": [
            "Manifest counts describe what the batch wrote and cannot detect an "
            "incomplete upstream fetch.",
            "Exact manifest/source comparison assumes a clean ingest and rejects "
            "incremental runs against an already populated generation.",
        ],
    }


def _run(args: argparse.Namespace) -> None:
    embedder = resolve_embedder()
    store = PgVectorStore(
        resolve_dsn(),
        profile=embedder.profile,
        mode=resolve_profile_mode(),
    )
    command = str(args.command)
    space_key = str(args.space)
    if command == "create-building":
        generation, status = store.create_building_generation_with_status(
            space_key=space_key,
            discard_inflight=bool(args.discard_inflight),
        )
        _json(
            {
                "generation_id": generation.generation_id,
                "space_key": generation.space_key,
                "state": generation.state.value,
                "status": status,
            }
        )
        return
    if command == "list":
        rows = store.generation_report(space_key=space_key)
        _json({"space_key": space_key, "generations": rows})
        return
    if command == "report":
        _json({"space_key": space_key, **_reference_report(store, space_key=space_key)})
        return
    if command == "validate":
        result = validate_generation(
            store,
            embedder,
            space_key=space_key,
            generation_id=int(args.generation_id),
            queries_path=args.queries,
        )
        _json(
            {
                "status": "sealed",
                "generation_id": result.generation_id,
                "checksum": result.checksum,
                "chunk_count": result.chunk_count,
                "manifest_revision": result.manifest_revision,
                "query_count": result.query_count,
                "reference": _reference_report(store, space_key=space_key),
                "notice": (
                    "Activation-time validation does not replace the post-activation "
                    "/ask golden run."
                ),
            }
        )
        return
    if command == "activate":
        generation = store.activate_generation(
            space_key=space_key, generation_id=int(args.generation_id)
        )
        _json(
            {
                "status": "active",
                "space_key": generation.space_key,
                "generation_id": generation.generation_id,
            }
        )
        return
    raise RuntimeError(f"Unhandled command: {command}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _run(args)
    except (
        EmbedderConfigurationError,
        ProfileModeConfigurationError,
        EmbeddingProfileMismatchError,
        ValidationQueryConfigurationError,
        DsnConfigurationError,
    ) as exc:
        print(exc, file=sys.stderr)
        return 2
    except GenerationValidationError as exc:
        print(exc, file=sys.stderr)
        return 3
    except GenerationTargetError as exc:
        print(exc, file=sys.stderr)
        return 4
    except (GenerationConcurrencyError, psycopg.errors.LockNotAvailable) as exc:
        print(exc, file=sys.stderr)
        return 5
    except psycopg.OperationalError as exc:
        print(f"Database connection failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
