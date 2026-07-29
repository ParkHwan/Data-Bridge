"""Fail-closed Cloud Run generation-job execution and result observation."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn, cast

CLI_PREFIX = "DATABRIDGE_RESULT "
WRAPPER_PREFIX = "DATABRIDGE_WRAPPER_RESULT "
_MISSING = object()
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
COMMAND_TIMEOUT_SECONDS = 30.0
CORRELATION_TIMEOUT_SECONDS = 120.0
TASK_READ_TIMEOUT_SECONDS = 60.0
LOG_READ_TIMEOUT_SECONDS = 30.0
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 3900

REASONS: dict[int, frozenset[str]] = {
    0: frozenset({"ok"}),
    1: frozenset({"database_unavailable", "unexpected_error"}),
    2: frozenset(
        {
            "dsn_missing",
            "profile_mismatch",
            "queries_sha_mismatch",
            "queries_contract_violation",
            "confirmation_missing",
            "cli_usage_error",
        }
    ),
    3: frozenset({"structural_validation_failed", "search_validation_failed"}),
    4: frozenset(
        {
            "target_not_found",
            "target_state_mismatch",
            "activation_integrity_failed",
            "legacy_cleanup_window_closed",
        }
    ),
    5: frozenset({"concurrent_change", "lock_timeout"}),
}

WRAPPER_REASONS: dict[int, frozenset[str]] = {
    80: frozenset({"wrapper_timeout"}),
    81: frozenset({"result_marker_missing"}),
    82: frozenset({"result_marker_mismatch"}),
    83: frozenset({"task_result_unavailable"}),
    84: frozenset({"task_terminated_by_signal"}),
    85: frozenset(
        {
            "wrapper_transport_error",
            "creation_confirmed_absent",
            "correlation_indeterminate",
        }
    ),
    86: frozenset({"unexpected_task_exit_code"}),
    87: frozenset({"duplicate_execution"}),
}


@dataclass(frozen=True, slots=True)
class TaskDecision:
    exit_code: int
    reason: str

    @property
    def is_cli(self) -> bool:
        return self.exit_code in REASONS


@dataclass(frozen=True, slots=True)
class MarkerResult:
    exit_code: int
    reason: str
    generation_id: int | None


class WrapperUsageError(RuntimeError):
    """A local wrapper argument error that occurs before jobs execute."""


class WrapperArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise WrapperUsageError(message)


class ReadStageDeadlineExceeded(RuntimeError):
    """A bounded read stage exhausted its wall-clock deadline."""


class ReadTransportFailure(RuntimeError):
    """A read command failed or hung until its retry budget was exhausted."""


def _positive_timeout_seconds(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise argparse.ArgumentTypeError("must be a positive integer")
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _is_json_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _evaluate_success_gate(task: Mapping[str, object]) -> TaskDecision:
    """Evaluate the one success gate shared by absent-exit and explicit-zero paths."""
    status_value = task.get("status")
    if not isinstance(status_value, Mapping):
        return TaskDecision(83, "task_result_unavailable")
    status = status_value
    if "completionTime" not in status or status["completionTime"] is None:
        return TaskDecision(83, "task_result_unavailable")
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return TaskDecision(83, "task_result_unavailable")
    completed = [
        item for item in conditions if isinstance(item, Mapping) and item.get("type") == "Completed"
    ]
    if len(completed) != 1:
        return TaskDecision(83, "task_result_unavailable")
    completed_status = completed[0].get("status")
    if not isinstance(completed_status, str) or completed_status not in {
        "True",
        "False",
    }:
        return TaskDecision(83, "task_result_unavailable")
    if completed_status != "True":
        return TaskDecision(83, "task_result_unavailable")
    attempt = status.get("lastAttemptResult")
    if not isinstance(attempt, Mapping):
        return TaskDecision(83, "task_result_unavailable")
    attempt_status = attempt.get("status")
    if not isinstance(attempt_status, Mapping):
        return TaskDecision(83, "task_result_unavailable")
    code = attempt_status.get("code", _MISSING)
    if code is not _MISSING and (not _is_json_int(code) or code != 0):
        return TaskDecision(83, "task_result_unavailable")
    return TaskDecision(0, "ok")


def evaluate_tasks(tasks: Sequence[Mapping[str, object]]) -> TaskDecision:
    if len(tasks) != 1:
        return TaskDecision(83, "task_result_unavailable")
    task = tasks[0]
    status_value = task.get("status")
    if not isinstance(status_value, Mapping):
        return TaskDecision(83, "task_result_unavailable")
    status = status_value
    attempt_value = status.get("lastAttemptResult")
    attempt: Mapping[str, object] = attempt_value if isinstance(attempt_value, Mapping) else {}
    exit_value = attempt.get("exitCode", _MISSING)
    term_value = attempt.get("termSignal", _MISSING)
    retried = status.get("retried", _MISSING)

    for value in (exit_value, term_value, retried):
        if value is not _MISSING and not _is_json_int(value):
            return TaskDecision(83, "task_result_unavailable")
    exit_code = cast(int, exit_value) if exit_value is not _MISSING else None
    term_signal = cast(int, term_value) if term_value is not _MISSING else None
    if retried is not _MISSING and retried != 0:
        return TaskDecision(83, "task_result_unavailable")
    if term_signal is not None and term_signal <= 0:
        return TaskDecision(83, "task_result_unavailable")
    if exit_code is not None and term_signal is not None:
        return TaskDecision(83, "task_result_unavailable")
    if term_signal is not None:
        return TaskDecision(84, "task_terminated_by_signal")
    if exit_code is None:
        return _evaluate_success_gate(task)
    if exit_code == 0:
        return _evaluate_success_gate(task)
    if 1 <= exit_code <= 5:
        return TaskDecision(exit_code, "cli")
    return TaskDecision(86, "unexpected_task_exit_code")


def _parse_prefixed_json(line: str, prefix: str) -> Mapping[str, object] | None:
    if not line.startswith(prefix):
        return None
    raw = line[len(prefix) :]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, Mapping):
        return None
    return value


def validate_cli_markers(
    lines: Sequence[str], *, operation_id: str, expected_exit: int
) -> tuple[MarkerResult | None, TaskDecision | None]:
    candidates = [line for line in lines if line.startswith(CLI_PREFIX)]
    if not candidates:
        return None, TaskDecision(81, "result_marker_missing")
    if len(candidates) != 1:
        return None, TaskDecision(82, "result_marker_mismatch")
    payload = _parse_prefixed_json(candidates[0], CLI_PREFIX)
    required = {
        "operation_id",
        "command",
        "space_key",
        "generation_id",
        "exit_code",
        "reason",
    }
    if payload is None or set(payload) != required:
        return None, TaskDecision(82, "result_marker_mismatch")
    marker_exit = payload["exit_code"]
    reason = payload["reason"]
    if (
        not _is_json_int(marker_exit)
        or marker_exit not in REASONS
        or marker_exit != expected_exit
        or not isinstance(reason, str)
        or reason not in REASONS[marker_exit]
        or not isinstance(payload["operation_id"], str)
        or _UUID.fullmatch(payload["operation_id"]) is None
        or payload["operation_id"] != operation_id
    ):
        return None, TaskDecision(82, "result_marker_mismatch")
    command = payload["command"]
    space_key = payload["space_key"]
    usage_error = marker_exit == 2 and reason == "cli_usage_error"
    allowed_commands = {
        "create-building",
        "list",
        "report",
        "validate",
        "inventory",
        "activate",
        "delete-legacy",
    }
    if isinstance(command, str) and command not in allowed_commands:
        return None, TaskDecision(82, "result_marker_mismatch")
    if usage_error:
        if command is not None and (not isinstance(command, str) or not command):
            return None, TaskDecision(82, "result_marker_mismatch")
        if space_key is not None and (not isinstance(space_key, str) or not space_key):
            return None, TaskDecision(82, "result_marker_mismatch")
    elif (
        not isinstance(command, str)
        or not command
        or command not in allowed_commands
        or not isinstance(space_key, str)
        or not space_key
    ):
        return None, TaskDecision(82, "result_marker_mismatch")
    generation_id = payload["generation_id"]
    if generation_id is not None and (
        not isinstance(generation_id, int) or isinstance(generation_id, bool) or generation_id <= 0
    ):
        return None, TaskDecision(82, "result_marker_mismatch")
    if isinstance(command, str):
        requires_generation = command in {
            "validate",
            "inventory",
            "activate",
            "delete-legacy",
        }
        if requires_generation and not usage_error and generation_id is None:
            return None, TaskDecision(82, "result_marker_mismatch")
        if command == "create-building" and marker_exit == 0 and generation_id is None:
            return None, TaskDecision(82, "result_marker_mismatch")
        if command in {"list", "report"} and generation_id is not None:
            return None, TaskDecision(82, "result_marker_mismatch")
    return MarkerResult(marker_exit, reason, generation_id), None


RunCommand = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


def _run_command(
    command: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=max(timeout_seconds, 0.001),
    )


def _gcloud_json(run: RunCommand, command: list[str], *, timeout_seconds: float) -> object:
    completed = run([*command, "--format=json"], timeout_seconds)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or "gcloud read failed")
    return json.loads(completed.stdout)


def _gcloud_json_retry(
    run: RunCommand,
    command: list[str],
    *,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    deadline: float,
    attempts: int = 3,
) -> object:
    last_error: Exception | None = None
    for attempt in range(attempts):
        remaining = deadline - monotonic()
        if remaining <= 0:
            if last_error is None:
                raise ReadStageDeadlineExceeded("gcloud read deadline exhausted")
            raise ReadTransportFailure("gcloud read failed before its deadline") from last_error
        try:
            return _gcloud_json(
                run,
                command,
                timeout_seconds=min(COMMAND_TIMEOUT_SECONDS, remaining),
            )
        except (OSError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise ReadTransportFailure(
                        "gcloud read failed before its deadline"
                    ) from last_error
                sleep(min(1.0, remaining))
    raise ReadTransportFailure("gcloud read retries exhausted") from last_error


def _contains_operation_id(value: object, operation_id: str) -> bool:
    if isinstance(value, Mapping):
        if value.get("name") == "DATABRIDGE_OPERATION_ID" and value.get("value") == operation_id:
            return True
        return any(_contains_operation_id(item, operation_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_operation_id(item, operation_id) for item in value)
    return False


def _correlate_execution(
    run: RunCommand,
    *,
    project: str,
    region: str,
    job: str,
    operation_id: str,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    deadline: float,
) -> tuple[str | None, TaskDecision | None]:
    base = ["gcloud", "run", "jobs", "executions"]
    listed = _gcloud_json_retry(
        run,
        [*base, "list", "--job", job, "--project", project, "--region", region],
        sleep=sleep,
        monotonic=monotonic,
        deadline=deadline,
    )
    if not isinstance(listed, list):
        raise RuntimeError("execution list did not return a JSON list")
    matches: list[str] = []
    indeterminate = False
    for item in listed:
        if not isinstance(item, Mapping):
            indeterminate = True
            continue
        metadata = item.get("metadata")
        name = metadata.get("name") if isinstance(metadata, Mapping) else None
        if not isinstance(name, str):
            indeterminate = True
            continue
        described = _gcloud_json_retry(
            run,
            [*base, "describe", name, "--project", project, "--region", region],
            sleep=sleep,
            monotonic=monotonic,
            deadline=deadline,
        )
        if not isinstance(described, Mapping):
            indeterminate = True
            continue
        if _contains_operation_id(described, operation_id):
            matches.append(name)
    if len(matches) > 1:
        return None, TaskDecision(87, "duplicate_execution")
    if indeterminate:
        return None, TaskDecision(85, "correlation_indeterminate")
    if len(matches) == 1:
        return matches[0], None
    return None, TaskDecision(85, "correlation_indeterminate")


def _execution_complete(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    status = value.get("status")
    if not isinstance(status, Mapping):
        return False
    if status.get("completionTime") is not None:
        return True
    conditions = status.get("conditions")
    return isinstance(conditions, list) and any(
        isinstance(item, Mapping)
        and item.get("type") == "Completed"
        and isinstance(item.get("status"), str)
        and item.get("status") in {"True", "False"}
        for item in conditions
    )


def _wrapper_payload(
    *, operation_id: str | None, decision: TaskDecision, marker: MarkerResult | None
) -> dict[str, object]:
    cli_valid = marker is not None and decision.exit_code in REASONS
    cli_exit = marker.exit_code if cli_valid and marker is not None else None
    cli_reason = marker.reason if cli_valid and marker is not None else None
    # The generation id is the one thing an operator cannot obtain any other way: the
    # marker carries it, but it is written to the job's log, not to this stdout. Without
    # it the rollout cannot name the generation it just created.
    cli_generation_id = marker.generation_id if cli_valid and marker is not None else None
    return {
        "operation_id": operation_id,
        "wrapper_exit": decision.exit_code,
        "wrapper_reason": "passthrough" if decision.exit_code in REASONS else decision.reason,
        "cli_exit": cli_exit,
        "cli_reason": cli_reason,
        "cli_generation_id": cli_generation_id,
    }


def validate_wrapper_result(
    lines: Sequence[str], *, process_exit: int
) -> Mapping[str, object] | None:
    """Validate the wrapper result before considering a manual rerun."""
    candidates = [line for line in lines if line.startswith(WRAPPER_PREFIX)]
    if len(candidates) != 1:
        return None
    payload = _parse_prefixed_json(candidates[0], WRAPPER_PREFIX)
    required = {
        "operation_id",
        "wrapper_exit",
        "wrapper_reason",
        "cli_exit",
        "cli_reason",
        "cli_generation_id",
    }
    if payload is None or set(payload) != required:
        return None
    wrapper_exit = payload["wrapper_exit"]
    wrapper_reason = payload["wrapper_reason"]
    if (
        not _is_json_int(wrapper_exit)
        or wrapper_exit != process_exit
        or not isinstance(wrapper_reason, str)
    ):
        return None
    if wrapper_exit in REASONS:
        cli_exit = payload["cli_exit"]
        cli_reason = payload["cli_reason"]
        if (
            wrapper_reason != "passthrough"
            or not _is_json_int(cli_exit)
            or cli_exit != wrapper_exit
            or not isinstance(cli_reason, str)
            or cli_reason not in REASONS[wrapper_exit]
        ):
            return None
        generation_id = payload["cli_generation_id"]
        if generation_id is not None and (
            not _is_json_int(generation_id) or cast(int, generation_id) <= 0
        ):
            return None
    elif wrapper_exit in WRAPPER_REASONS:
        if (
            wrapper_reason not in WRAPPER_REASONS[wrapper_exit]
            or payload["cli_exit"] is not None
            or payload["cli_reason"] is not None
            or payload["cli_generation_id"] is not None
        ):
            return None
    else:
        return None
    operation_id = payload["operation_id"]
    if operation_id is None:
        if wrapper_exit != 85 or wrapper_reason != "creation_confirmed_absent":
            return None
    elif not isinstance(operation_id, str) or _UUID.fullmatch(operation_id) is None:
        return None
    return payload


def execute(
    *,
    project: str,
    region: str,
    job: str,
    job_args: Sequence[str],
    run: RunCommand = _run_command,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
) -> tuple[TaskDecision, MarkerResult | None, str]:
    operation_id = str(uuid.uuid4())
    command = [
        "gcloud",
        "run",
        "jobs",
        "execute",
        job,
        "--project",
        project,
        "--region",
        region,
        f"--update-env-vars=DATABRIDGE_OPERATION_ID={operation_id}",
        f"--args={','.join(job_args)}",
        "--format=value(metadata.name)",
    ]
    try:
        created = run(
            command, COMMAND_TIMEOUT_SECONDS
        )  # Deliberately exactly once: this call is non-idempotent.
    except OSError:
        return TaskDecision(85, "creation_confirmed_absent"), None, operation_id
    except subprocess.TimeoutExpired:
        created = subprocess.CompletedProcess(command, 1, "", "jobs execute timed out")
    execution: str | None = created.stdout.strip() if created.returncode == 0 else None
    if not execution:
        try:
            execution, correlation = _correlate_execution(
                run,
                project=project,
                region=region,
                job=job,
                operation_id=operation_id,
                sleep=sleep,
                monotonic=monotonic,
                deadline=monotonic() + CORRELATION_TIMEOUT_SECONDS,
            )
        except (
            ReadStageDeadlineExceeded,
            ReadTransportFailure,
            RuntimeError,
            json.JSONDecodeError,
        ):
            return TaskDecision(85, "wrapper_transport_error"), None, operation_id
        if correlation is not None:
            return correlation, None, operation_id
        assert execution is not None

    deadline = monotonic() + timeout_seconds
    while True:
        if monotonic() >= deadline:
            return TaskDecision(80, "wrapper_timeout"), None, operation_id
        try:
            state = _gcloud_json_retry(
                run,
                [
                    "gcloud",
                    "run",
                    "jobs",
                    "executions",
                    "describe",
                    execution,
                    "--project",
                    project,
                    "--region",
                    region,
                ],
                sleep=sleep,
                monotonic=monotonic,
                deadline=deadline,
            )
        except ReadStageDeadlineExceeded:
            return TaskDecision(80, "wrapper_timeout"), None, operation_id
        except (ReadTransportFailure, RuntimeError, json.JSONDecodeError):
            return TaskDecision(85, "wrapper_transport_error"), None, operation_id
        if _execution_complete(state):
            break
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(1.0, remaining))

    task_deadline = monotonic() + TASK_READ_TIMEOUT_SECONDS
    try:
        names = _gcloud_json_retry(
            run,
            [
                "gcloud",
                "run",
                "jobs",
                "executions",
                "tasks",
                "list",
                "--execution",
                execution,
                "--project",
                project,
                "--region",
                region,
            ],
            sleep=sleep,
            monotonic=monotonic,
            deadline=task_deadline,
        )
        if not isinstance(names, list):
            raise RuntimeError("task list did not return a JSON list")
        if len(names) != 1:
            return TaskDecision(83, "task_result_unavailable"), None, operation_id
        item = names[0]
        if not isinstance(item, Mapping):
            return TaskDecision(83, "task_result_unavailable"), None, operation_id
        metadata = item.get("metadata")
        task_name = metadata.get("name") if isinstance(metadata, Mapping) else None
        if not isinstance(task_name, str):
            return TaskDecision(83, "task_result_unavailable"), None, operation_id
        described = _gcloud_json_retry(
            run,
            [
                "gcloud",
                "run",
                "jobs",
                "executions",
                "tasks",
                "describe",
                task_name,
                "--project",
                project,
                "--region",
                region,
            ],
            sleep=sleep,
            monotonic=monotonic,
            deadline=task_deadline,
        )
        if not isinstance(described, Mapping):
            return TaskDecision(83, "task_result_unavailable"), None, operation_id
        tasks = [described]
    except (
        ReadStageDeadlineExceeded,
        ReadTransportFailure,
        RuntimeError,
        json.JSONDecodeError,
    ):
        return TaskDecision(85, "wrapper_transport_error"), None, operation_id

    decision = evaluate_tasks(tasks)
    if not decision.is_cli:
        return decision, None, operation_id
    log_command = [
        "gcloud",
        "logging",
        "read",
        (
            'logName:"run.googleapis.com%2Fstdout" '
            f'labels."run.googleapis.com/execution_name"="{execution}" '
            'labels."run.googleapis.com/task_index"="0" '
            'labels."run.googleapis.com/task_attempt"="0"'
        ),
        "--project",
        project,
        "--limit=100",
    ]
    lines: list[str] = []
    log_deadline = monotonic() + LOG_READ_TIMEOUT_SECONDS
    for attempt in range(10):
        if monotonic() >= log_deadline:
            break
        try:
            logs = _gcloud_json_retry(
                run,
                log_command,
                sleep=sleep,
                monotonic=monotonic,
                deadline=log_deadline,
            )
        except ReadStageDeadlineExceeded:
            # Every read succeeded; the eventual-consistency window simply ran out.
            # That is not a transport failure — judge on the lines collected so far,
            # which yields 81 when the marker never appeared. Returning 85 here would
            # make the outcome depend on which deadline check happened to fire first.
            break
        except (ReadTransportFailure, RuntimeError):
            return TaskDecision(85, "wrapper_transport_error"), None, operation_id
        if not isinstance(logs, list):
            # The canonical logging response is a JSON list. Anything else means we did
            # not read the log, which is different from reading an empty log.
            return TaskDecision(85, "wrapper_transport_error"), None, operation_id
        lines = [
            str(item["textPayload"])
            for item in logs
            if isinstance(item, Mapping) and "textPayload" in item
        ]
        if any(line.startswith(CLI_PREFIX) for line in lines):
            break
        if attempt < 9:
            remaining = log_deadline - monotonic()
            if remaining > 0:
                sleep(min(1.0, remaining))
    marker, marker_error = validate_cli_markers(
        lines, operation_id=operation_id, expected_exit=decision.exit_code
    )
    if marker_error is not None:
        return marker_error, None, operation_id
    assert marker is not None
    return TaskDecision(marker.exit_code, marker.reason), marker, operation_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = WrapperArgumentParser(description="Run and observe a Data Bridge generation job")
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--job", default="databridge-generation")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_timeout_seconds,
        default=DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        help=(
            "execution polling deadline in seconds "
            f"(default: {DEFAULT_EXECUTION_TIMEOUT_SECONDS}; "
            "3600s task timeout plus 300s observation margin)"
        ),
    )
    parser.add_argument("job_args", nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(argv)
    except WrapperUsageError as exc:
        print(exc, file=sys.stderr)
        decision = TaskDecision(85, "creation_confirmed_absent")
        print(
            WRAPPER_PREFIX
            + json.dumps(
                _wrapper_payload(operation_id=None, decision=decision, marker=None),
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )
        return decision.exit_code
    job_args = list(args.job_args)
    if job_args[:1] == ["--"]:
        job_args = job_args[1:]
    decision, marker, operation_id = execute(
        project=str(args.project),
        region=str(args.region),
        job=str(args.job),
        job_args=job_args,
        timeout_seconds=float(args.timeout_seconds),
    )
    print(
        WRAPPER_PREFIX
        + json.dumps(
            _wrapper_payload(operation_id=operation_id, decision=decision, marker=marker),
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )
    return decision.exit_code


if __name__ == "__main__":
    sys.exit(main())
