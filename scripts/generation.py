"""Operate generation lifecycle and activation-gating validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

import psycopg
from psycopg.conninfo import conninfo_to_dict

from databridge.embed import EmbedderConfigurationError, resolve_embedder
from databridge.store import (
    ActivationIntegrityError,
    EmbeddingProfileMismatchError,
    GenerationConcurrencyError,
    GenerationTargetError,
    GenerationTargetNotFoundError,
    GenerationTargetStateMismatchError,
    GenerationValidationError,
    LegacyCleanupWindowClosedError,
    PgVectorStore,
    ProfileModeConfigurationError,
    SearchGenerationValidationError,
    StructuralGenerationValidationError,
    ValidationQueryConfigurationError,
    ValidationQueryShaMismatchError,
    resolve_profile_mode,
)
from databridge.store.validation import validate_generation


class DsnConfigurationError(RuntimeError):
    """Raised when the generation CLI has no usable database configuration."""


class CliUsageError(RuntimeError):
    """Raised instead of argparse's unobservable SystemExit(2)."""


class MarkerArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


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
    parser = MarkerArgumentParser(description="Manage Data Bridge embedding generations")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-building")
    create.add_argument("--space", required=True)
    create.add_argument("--discard-inflight", action="store_true")

    for name in ("list", "report"):
        command = commands.add_parser(name)
        command.add_argument("--space", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--space", required=True)
    validate.add_argument("--generation-id", required=True, type=_positive_int)
    validate.add_argument("--queries", required=True, type=Path)
    validate.add_argument("--expected-queries-sha256", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("--space", required=True)
    inventory.add_argument("--generation-id", required=True, type=_positive_int)

    activate = commands.add_parser("activate")
    activate.add_argument("--space", required=True)
    activate.add_argument("--generation-id", required=True, type=_positive_int)
    activate.add_argument("--yes", action="store_true")

    delete_legacy = commands.add_parser(
        "delete-legacy", help="first-cutover-only legacy chunk cleanup"
    )
    delete_legacy.add_argument("--space", required=True)
    delete_legacy.add_argument("--generation-id", required=True, type=_positive_int)
    delete_legacy.add_argument("--yes", action="store_true")
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


def _run(args: argparse.Namespace) -> int | None:
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
        return generation.generation_id
    if command == "list":
        rows = store.generation_report(space_key=space_key)
        _json({"space_key": space_key, "generations": rows})
        return None
    if command == "report":
        _json({"space_key": space_key, **_reference_report(store, space_key=space_key)})
        return None
    if command == "inventory":
        inventory = store.generation_inventory(
            space_key=space_key, generation_id=int(args.generation_id)
        )
        _json(
            {
                "space_key": inventory.space_key,
                "generation_id": inventory.generation_id,
                "generation_state": inventory.generation_state.value,
                "profile_fingerprint": inventory.profile_fingerprint,
                "total_chunks": inventory.total_chunks,
                "sources": {
                    source_id: {
                        "chunk_count": source.chunk_count,
                        "headings": source.headings,
                    }
                    for source_id, source in inventory.sources.items()
                },
            }
        )
        return inventory.generation_id
    if command == "validate":
        result = validate_generation(
            store,
            embedder,
            space_key=space_key,
            generation_id=int(args.generation_id),
            queries_path=args.queries,
            expected_queries_sha256=str(args.expected_queries_sha256),
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
        return result.generation_id
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
        return generation.generation_id
    if command == "delete-legacy":
        cleanup_result = store.delete_legacy_chunks(
            space_key=space_key, generation_id=int(args.generation_id)
        )
        _json(
            {
                "space_key": cleanup_result.space_key,
                "generation_id": cleanup_result.generation_id,
                "legacy_count_before": cleanup_result.legacy_count_before,
                "deleted_count": cleanup_result.deleted_count,
            }
        )
        return cleanup_result.generation_id
    raise RuntimeError(f"Unhandled command: {command}")


def _marker_context(args: argparse.Namespace | None) -> tuple[str | None, str | None, int | None]:
    if args is None:
        return None, None, None
    command_value = getattr(args, "command", None)
    space_value = getattr(args, "space", None)
    command = command_value if isinstance(command_value, str) and command_value else None
    space_key = space_value if isinstance(space_value, str) and space_value else None
    generation_value = getattr(args, "generation_id", None)
    generation_id = (
        generation_value
        if isinstance(generation_value, int)
        and not isinstance(generation_value, bool)
        and generation_value > 0
        else None
    )
    if command in {"list", "report"}:
        generation_id = None
    return command, space_key, generation_id


def _emit_marker(
    *,
    args: argparse.Namespace | None,
    generation_id: int | None,
    exit_code: int,
    reason: str,
) -> None:
    command, space_key, parsed_generation_id = _marker_context(args)
    operation_id = os.environ.get("DATABRIDGE_OPERATION_ID")
    payload = {
        "operation_id": operation_id.lower() if operation_id is not None else None,
        "command": command,
        "space_key": space_key,
        "generation_id": generation_id if generation_id is not None else parsed_generation_id,
        "exit_code": exit_code,
        "reason": reason,
    }
    print(
        "DATABRIDGE_RESULT "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    generation_id: int | None = None
    exit_code = 0
    reason = "ok"
    try:
        args = _parser().parse_args(argv)
        if args.command in {"activate", "delete-legacy"} and not args.yes:
            raise CliUsageError("confirmation_missing")
        generation_id = _run(args)
    except CliUsageError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = (
            2,
            ("confirmation_missing" if str(exc) == "confirmation_missing" else "cli_usage_error"),
        )
    except DsnConfigurationError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 2, "dsn_missing"
    except ValidationQueryShaMismatchError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 2, "queries_sha_mismatch"
    except ValidationQueryConfigurationError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 2, "queries_contract_violation"
    except (
        EmbedderConfigurationError,
        ProfileModeConfigurationError,
        EmbeddingProfileMismatchError,
    ) as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 2, "profile_mismatch"
    except StructuralGenerationValidationError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 3, "structural_validation_failed"
    except SearchGenerationValidationError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 3, "search_validation_failed"
    except GenerationValidationError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 3, "structural_validation_failed"
    except GenerationTargetNotFoundError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 4, "target_not_found"
    except GenerationTargetStateMismatchError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 4, "target_state_mismatch"
    except ActivationIntegrityError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 4, "activation_integrity_failed"
    except LegacyCleanupWindowClosedError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 4, "legacy_cleanup_window_closed"
    except GenerationTargetError as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 4, "target_state_mismatch"
    except (GenerationConcurrencyError, psycopg.errors.LockNotAvailable) as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = (
            5,
            "lock_timeout"
            if isinstance(exc, psycopg.errors.LockNotAvailable)
            else "concurrent_change",
        )
    except psycopg.OperationalError as exc:
        print(f"Database connection failed: {exc}", file=sys.stderr)
        exit_code, reason = 1, "database_unavailable"
    except Exception as exc:
        print(exc, file=sys.stderr)
        exit_code, reason = 1, "unexpected_error"
    _emit_marker(
        args=args,
        generation_id=generation_id,
        exit_code=exit_code,
        reason=reason,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
