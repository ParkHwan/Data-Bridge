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
    check_report,
    correlate_execution,
    read_generation_id,
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
    project, region = str(args.project), str(args.region)
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
    common = ["--project", str(args.project), "--region", str(args.region)]
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


def _report(args: argparse.Namespace) -> int:
    value = json.loads(_read_text(str(args.input)))
    return _report_problems(
        check_report(
            _object(value, label="generation report"),
            expected_generation=int(args.expected_generation),
        )
    )


def _correlate(args: argparse.Namespace) -> int:
    common = ["--project", str(args.project), "--region", str(args.region)]
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run tested generation-rollout checks")
    commands = parser.add_subparsers(dest="command", required=True)

    preconditions = commands.add_parser("preconditions")
    preconditions.add_argument("--project", required=True)
    preconditions.add_argument("--region", required=True)
    preconditions.add_argument("--job", default=DEFAULT_JOB)
    preconditions.add_argument("--service", default=DEFAULT_SERVICE)
    preconditions.add_argument("--instance", default=DEFAULT_INSTANCE)
    preconditions.set_defaults(handler=_preconditions)

    images = commands.add_parser("images")
    images.add_argument("--project", required=True)
    images.add_argument("--region", required=True)
    images.add_argument("--expected-image", required=True)
    images.add_argument("--service", default=DEFAULT_SERVICE)
    images.set_defaults(handler=_images)

    report = commands.add_parser("report")
    report.add_argument("--input", default="-", help="JSON report file, or - for stdin")
    report.add_argument("--expected-generation", required=True, type=int)
    report.set_defaults(handler=_report)

    correlate = commands.add_parser("correlate")
    correlate.add_argument("--project", required=True)
    correlate.add_argument("--region", required=True)
    correlate.add_argument("--job", default=DEFAULT_JOB)
    correlate.add_argument("--operation-id", required=True)
    correlate.set_defaults(handler=_correlate)

    generation_id = commands.add_parser("generation-id")
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
