#!/usr/bin/env python3
"""Fetch rollout evidence and evaluate it with the tested pure checks."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from databridge.rollout_checks import (
    ROLLOUT_JOBS,
    RolloutCheckError,
    check_generation_job,
    check_images,
    check_ingest_scope,
    check_report,
    check_rollback_is_safe,
    check_strict_mode,
    check_writers_quiesced,
    correlate_execution,
    extract_report_body,
    read_generation_id,
    read_operation_id,
    read_serving_revision,
)

DEFAULT_JOB = "databridge-generation"
DEFAULT_SERVICE = "databridge"
DEFAULT_INSTANCE = "databridge-demo"

def _run(command: Sequence[str]) -> str:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        # gcloud resource output can contain literal environment values. Do not echo either
        # stream here; the command name is enough for an operator to reproduce it deliberately.
        raise RolloutCheckError(f"command failed: {' '.join(command[:5])}")
    return completed.stdout


def _gcloud_json(arguments: Sequence[str]) -> object:
    return json.loads(_run(["gcloud", *arguments, "--format=json"]))


def _gcloud_value(arguments: Sequence[str], field: str) -> str:
    return _run(["gcloud", *arguments, f"--format=value({field})"]).strip()


def _object(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RolloutCheckError(f"{label} did not return a JSON object")
    return value


def _list(value: object, *, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise RolloutCheckError(f"{label} did not return a JSON list")
    return value


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text("utf-8")


def _report_problems(problems: Sequence[str]) -> int:
    if not problems:
        print("rollout check OK")
        return 0
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    return 1


def _preconditions(args: argparse.Namespace) -> int:
    """Everything the runbook must establish before the first destructive step.

    This is a drop-in replacement for the runbook's own preflight, so it has to cover
    the whole set: a partial check that prints OK is the failure this module exists to
    remove.
    """
    project, region = _require_location(args)
    problems: list[str] = []

    value = _gcloud_json(
        ["run", "jobs", "describe", str(args.job), "--project", project, "--region", region]
    )
    problems.extend(check_generation_job(_object(value, label="generation job")))

    if not _gcloud_value(
        ["run", "services", "describe", str(args.service),
         "--project", project, "--region", region],
        "status.url",
    ):
        problems.append("the service has no URL; it is not serving")

    # Reading Tasks is a different permission from listing executions, and the wrapper
    # needs it. Probe it for real when an execution exists.
    execution = _gcloud_value(
        [
            "run", "jobs", "executions", "list", "--job", str(args.job),
            "--project", project, "--region", region, "--limit", "1",
        ],
        "metadata.name",
    )
    if execution:
        task = _gcloud_value(
            [
                "run", "jobs", "executions", "tasks", "list", "--execution", execution,
                "--project", project, "--region", region, "--limit", "1",
            ],
            "metadata.name",
        )
        if task:
            _gcloud_value(
                ["run", "jobs", "executions", "tasks", "describe", task,
                 "--project", project, "--region", region],
                "metadata.name",
            )
        else:
            problems.append(
                f"execution {execution} exposes no task, so run.tasks.get was not exercised; "
                "run one read-only command through the wrapper before continuing"
            )
    else:
        problems.append(
            "no execution exists yet, so task read access is unproven; "
            "run one read-only command through the wrapper before continuing"
        )

    if _gcloud_value(
        ["sql", "instances", "describe", str(args.instance), "--project", project],
        "settings.backupConfiguration.pointInTimeRecoveryEnabled",
    ) != "True":
        problems.append("point-in-time recovery is not enabled")
    # Enabled is not usable: the window has to exist and be readable.
    if not _run(
        ["gcloud", "sql", "instances", "get-latest-recovery-time", str(args.instance),
         "--project", project]
    ).strip():
        problems.append("the recovery window is empty or unreadable")

    return _report_problems(problems)


def _images(args: argparse.Namespace) -> int:
    project, region = _require_location(args)
    common = ["--project", project, "--region", region]
    service = _gcloud_value(
        ["run", "services", "describe", str(args.service), *common],
        "spec.template.spec.containers[0].image",
    )
    job_names = ROLLOUT_JOBS
    jobs = {
        name: _gcloud_value(
            ["run", "jobs", "describe", name, *common],
            "spec.template.spec.template.spec.containers[0].image",
        )
        for name in job_names
    }
    return _report_problems(
        check_images(expected=str(args.expected_image), service=service, jobs=jobs)
    )


def _env_entries(value: object, *, label: str) -> list[Mapping[str, object]]:
    """Return every env entry, refusing to drop one we cannot read.

    Silently skipping a malformed entry would let a check succeed beside something it
    could not inspect — the same distinction between absence and inability to observe
    that the wrapper draws.
    """
    if not isinstance(value, list):
        raise RolloutCheckError(f"{label} env is not a list")
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise RolloutCheckError(f"{label} env[{index}] is not an object")
    return [item for item in value if isinstance(item, Mapping)]


def _job_env(name: str, project: str, region: str) -> list[Mapping[str, object]]:
    value = _gcloud_json(
        ["run", "jobs", "describe", name, "--project", project, "--region", region]
    )
    job = _object(value, label=f"job {name}")
    spec = _object(job.get("spec"), label=f"job {name} spec")
    template = _object(spec.get("template"), label=f"job {name} template")
    execution = _object(template.get("spec"), label=f"job {name} execution spec")
    task_template = _object(execution.get("template"), label=f"job {name} task template")
    task = _object(task_template.get("spec"), label=f"job {name} task spec")
    containers = task.get("containers")
    if not isinstance(containers, list) or not containers:
        raise RolloutCheckError(f"job {name} has no container")
    container = _object(containers[0], label=f"job {name} container")
    return _env_entries(container.get("env", []), label=f"job {name}")


def _service_env(name: str, project: str, region: str) -> list[Mapping[str, object]]:
    value = _gcloud_json(
        ["run", "services", "describe", name, "--project", project, "--region", region]
    )
    service = _object(value, label="service")
    spec = _object(service.get("spec"), label="service spec")
    template = _object(spec.get("template"), label="service template")
    revision = _object(template.get("spec"), label="service revision spec")
    containers = revision.get("containers")
    if not isinstance(containers, list) or not containers:
        raise RolloutCheckError("service has no container")
    container = _object(containers[0], label="service container")
    return _env_entries(container.get("env", []), label="service")


def _ingest_scope(args: argparse.Namespace) -> int:
    project, region = _require_location(args)
    env = _job_env(str(args.job), project, region)
    return _report_problems(
        check_ingest_scope(env, space_key=str(args.space), folder_id=str(args.folder_id))
    )


def _strict(args: argparse.Namespace) -> int:
    project, region = _require_location(args)
    return _report_problems(
        check_strict_mode(
            service_env=_service_env(str(args.service), project, region),
            job_envs={name: _job_env(name, project, region) for name in ROLLOUT_JOBS},
        )
    )


def _operation_id(args: argparse.Namespace) -> int:
    print(read_operation_id(_read_text(str(args.input)).splitlines()))
    return 0


def _quiesced(args: argparse.Namespace) -> int:
    project, region = _require_location(args)
    value = _gcloud_json(
        ["run", "jobs", "executions", "list", "--job", str(args.job),
         "--project", project, "--region", region]
    )
    if not isinstance(value, list):
        raise RolloutCheckError("execution list did not return a JSON list")
    return _report_problems(check_writers_quiesced(value))


def _serving_revision(args: argparse.Namespace) -> int:
    project, region = _require_location(args)
    value = _gcloud_json(
        ["run", "services", "describe", str(args.service), "--project", project,
         "--region", region]
    )
    service = _object(value, label="service")
    status = _object(service.get("status"), label="service status")
    traffic = status.get("traffic")
    if not isinstance(traffic, list):
        raise RolloutCheckError("service traffic is missing or is not a list")
    entries = [item for item in traffic if isinstance(item, Mapping)]
    if len(entries) != len(traffic):
        raise RolloutCheckError("a traffic entry is not an object")
    print(read_serving_revision(entries))
    return 0


def _rollback_safe(args: argparse.Namespace) -> int:
    return _report_problems(
        check_rollback_is_safe(
            inventory=extract_report_body(_read_text(str(args.inventory)).splitlines()),
            report=extract_report_body(_read_text(str(args.report)).splitlines()),
        )
    )


def _report(args: argparse.Namespace) -> int:
    report = extract_report_body(_read_text(str(args.input)).splitlines())
    return _report_problems(
        check_report(
            report,
            expected_generation=int(args.expected_generation),
        )
    )


def _correlate(args: argparse.Namespace) -> int:
    project, region = _require_location(args)
    common = ["--project", project, "--region", region]
    listed = _list(
        _gcloud_json(
            [
                "run",
                "jobs",
                "executions",
                "list",
                "--job",
                str(args.job),
                *common,
            ]
        ),
        label="execution list",
    )
    described: list[Mapping[str, object]] = []
    for index, item in enumerate(listed):
        candidate = _object(item, label=f"execution candidate {index}")
        metadata = _object(candidate.get("metadata"), label=f"execution candidate {index} metadata")
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise RolloutCheckError(f"execution candidate {index} has no name")
        description = _gcloud_json(
            ["run", "jobs", "executions", "describe", name, *common]
        )
        described.append(_object(description, label=f"execution {name}"))
    print(correlate_execution(described, operation_id=str(args.operation_id)))
    return 0


def _generation_id(args: argparse.Namespace) -> int:
    print(read_generation_id(_read_text(str(args.input)).splitlines()))
    return 0


def _add(commands: argparse._SubParsersAction[argparse.ArgumentParser], name: str):
    """Add a subcommand that always accepts --project and --region.

    Offline subcommands ignore them. Accepting them everywhere lets the runbook wrap the
    script once instead of remembering which subcommands take which flags — an exception
    an operator has to remember is another place for this to go wrong.
    """
    command = commands.add_parser(name)
    command.add_argument("--project", default=None)
    command.add_argument("--region", default=None)
    return command


def _require_location(args: argparse.Namespace) -> tuple[str, str]:
    if not args.project or not args.region:
        raise RolloutCheckError("--project and --region are required for this check")
    return str(args.project), str(args.region)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run tested generation-rollout checks")
    commands = parser.add_subparsers(dest="command", required=True)

    preconditions = _add(commands, "preconditions")
    preconditions.add_argument("--job", default=DEFAULT_JOB)
    preconditions.add_argument("--service", default=DEFAULT_SERVICE)
    preconditions.add_argument("--instance", default=DEFAULT_INSTANCE)
    preconditions.set_defaults(handler=_preconditions)

    images = _add(commands, "images")
    images.add_argument("--expected-image", required=True)
    images.add_argument("--service", default=DEFAULT_SERVICE)
    images.set_defaults(handler=_images)

    report = _add(commands, "report")
    report.add_argument("--input", default="-", help="JSON report file, or - for stdin")
    report.add_argument("--expected-generation", required=True, type=int)
    report.set_defaults(handler=_report)

    correlate = _add(commands, "correlate")
    correlate.add_argument("--job", default=DEFAULT_JOB)

    scope = _add(commands, "ingest-scope")
    scope.add_argument("--job", default="databridge-confluence-ingest")
    scope.add_argument("--space", required=True)
    scope.add_argument("--folder-id", required=True)
    scope.set_defaults(handler=_ingest_scope)

    strict = _add(commands, "strict")
    strict.add_argument("--service", default=DEFAULT_SERVICE)
    strict.set_defaults(handler=_strict)

    quiesced = _add(commands, "writers-quiesced")
    quiesced.add_argument("--job", default="databridge-confluence-ingest")
    quiesced.set_defaults(handler=_quiesced)

    serving = _add(commands, "serving-revision")
    serving.add_argument("--service", default=DEFAULT_SERVICE)
    serving.set_defaults(handler=_serving_revision)

    rollback = _add(commands, "rollback-safe")
    rollback.add_argument("--inventory", required=True)
    rollback.add_argument("--report", required=True)
    rollback.set_defaults(handler=_rollback_safe)

    operation = _add(commands, "operation-id")
    operation.add_argument("--input", default="-")
    operation.set_defaults(handler=_operation_id)
    correlate.add_argument("--operation-id", required=True)
    correlate.set_defaults(handler=_correlate)

    generation_id = _add(commands, "generation-id")
    generation_id.add_argument("--input", default="-", help="wrapper output file, or - for stdin")
    generation_id.set_defaults(handler=_generation_id)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler = args.handler
    if not callable(handler):  # pragma: no cover - argparse invariant
        raise RuntimeError("subcommand has no handler")
    try:
        return int(handler(args))
    except (json.JSONDecodeError, OSError, RolloutCheckError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
