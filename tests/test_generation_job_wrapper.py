"""Contracts for fail-closed generation-job result observation."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from copy import deepcopy

import pytest

from databridge import generation_job


def _task(*, exit_code: object = generation_job._MISSING) -> dict[str, object]:
    attempt: dict[str, object] = {"status": {}}
    if exit_code is not generation_job._MISSING:
        attempt["exitCode"] = exit_code
    return {
        "status": {
            "completionTime": "2026-07-30T00:00:00Z",
            "conditions": [{"type": "Completed", "status": "True"}],
            "lastAttemptResult": attempt,
        }
    }


def _set_path(task: dict[str, object], case: str) -> None:
    status = task["status"]
    assert isinstance(status, dict)
    attempt = status["lastAttemptResult"]
    assert isinstance(attempt, dict)
    if case == "completion_missing":
        status.pop("completionTime")
    elif case == "completed_missing":
        status["conditions"] = []
    elif case == "completed_multiple":
        status["conditions"] = [
            {"type": "Completed", "status": "True"},
            {"type": "Completed", "status": "True"},
        ]
    elif case == "completed_invalid":
        status["conditions"] = [{"type": "Completed", "status": True}]
    elif case == "completed_false":
        status["conditions"] = [{"type": "Completed", "status": "False"}]
    elif case == "attempt_missing":
        status.pop("lastAttemptResult")
    elif case == "status_missing":
        attempt.pop("status")
    elif case == "status_non_ok":
        attempt["status"] = {"code": 4}
    elif case == "contradiction":
        status["conditions"] = [{"type": "Completed", "status": "False"}]
        attempt["status"] = {"code": 0}
    else:  # pragma: no cover - test helper guard
        raise AssertionError(case)


FAILURE_CASES = (
    "completion_missing",
    "completed_missing",
    "completed_multiple",
    "completed_invalid",
    "completed_false",
    "attempt_missing",
    "status_missing",
    "status_non_ok",
    "contradiction",
)


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_success_gate_helper_rejects_each_failure(case: str) -> None:
    task = _task()
    _set_path(task, case)
    assert generation_job._evaluate_success_gate(task).exit_code == 83


@pytest.mark.parametrize("exit_code", [generation_job._MISSING, 0])
def test_both_success_entries_call_and_obey_one_helper(
    monkeypatch: pytest.MonkeyPatch, exit_code: object
) -> None:
    calls: list[Mapping[str, object]] = []

    def fail(task: Mapping[str, object]) -> generation_job.TaskDecision:
        calls.append(task)
        return generation_job.TaskDecision(83, "task_result_unavailable")

    monkeypatch.setattr(generation_job, "_evaluate_success_gate", fail)
    assert generation_job.evaluate_tasks([_task(exit_code=exit_code)]).exit_code == 83
    assert len(calls) == 1


@pytest.mark.parametrize("case", FAILURE_CASES)
@pytest.mark.parametrize("exit_code", [generation_job._MISSING, 0])
def test_full_failure_table_for_both_success_entries(case: str, exit_code: object) -> None:
    task = _task(exit_code=exit_code)
    _set_path(task, case)
    assert generation_job.evaluate_tasks([task]).exit_code == 83


@pytest.mark.parametrize("exit_code", [generation_job._MISSING, 0])
@pytest.mark.parametrize("status", [{}, {"code": 0}])
def test_four_positive_success_gate_cases(exit_code: object, status: dict[str, int]) -> None:
    task = _task(exit_code=exit_code)
    task_status = task["status"]
    assert isinstance(task_status, dict)
    attempt = task_status["lastAttemptResult"]
    assert isinstance(attempt, dict)
    attempt["status"] = status
    assert generation_job.evaluate_tasks([task]).exit_code == 0


@pytest.mark.parametrize(
    ("mutations", "expected"),
    [
        ({"exitCode": False}, 83),
        ({"exitCode": True}, 83),
        ({"exitCode": "0"}, 83),
        ({"exitCode": None}, 83),
        ({"termSignal": False}, 83),
        ({"termSignal": True}, 83),
        ({"termSignal": -9}, 83),
        ({"termSignal": 0}, 83),
        ({"termSignal": "9"}, 83),
        ({"termSignal": 9}, 84),
        ({"termSignal": 9, "exitCode": 3}, 83),
        ({"exitCode": 137}, 86),
    ],
)
def test_raw_attempt_state_boundaries(mutations: dict[str, object], expected: int) -> None:
    task = _task()
    status = task["status"]
    assert isinstance(status, dict)
    attempt = status["lastAttemptResult"]
    assert isinstance(attempt, dict)
    attempt.update(mutations)
    assert generation_job.evaluate_tasks([task]).exit_code == expected


@pytest.mark.parametrize(("retried", "expected"), [(False, 83), (True, 83), (0, 0), (1, 83)])
def test_retried_types(retried: object, expected: int) -> None:
    task = _task()
    status = task["status"]
    assert isinstance(status, dict)
    status["retried"] = retried
    assert generation_job.evaluate_tasks([task]).exit_code == expected


def test_measured_success_omits_all_three_raw_keys() -> None:
    task = _task()
    assert generation_job.evaluate_tasks([task]).exit_code == 0


def test_measured_timeout_and_cancel_are_83() -> None:
    timeout = _task()
    timeout_status = timeout["status"]
    assert isinstance(timeout_status, dict)
    timeout_status["conditions"] = [{"type": "Completed", "status": "False"}]
    attempt = timeout_status["lastAttemptResult"]
    assert isinstance(attempt, dict)
    attempt["status"] = {"code": 4}

    cancelled = deepcopy(timeout)
    cancelled_status = cancelled["status"]
    assert isinstance(cancelled_status, dict)
    cancelled_status.pop("completionTime")
    cancelled_attempt = cancelled_status["lastAttemptResult"]
    assert isinstance(cancelled_attempt, dict)
    cancelled_attempt["status"] = {"code": 1}
    assert generation_job.evaluate_tasks([timeout]).exit_code == 83
    assert generation_job.evaluate_tasks([cancelled]).exit_code == 83


@pytest.mark.parametrize("bad_status", [True, 1, "true"])
def test_completed_requires_exact_true_string(bad_status: object) -> None:
    task = _task()
    status = task["status"]
    assert isinstance(status, dict)
    status["conditions"] = [{"type": "Completed", "status": bad_status}]
    assert generation_job.evaluate_tasks([task]).exit_code == 83


def test_completed_type_must_be_exact_string() -> None:
    task = _task()
    status = task["status"]
    assert isinstance(status, dict)
    status["conditions"] = [{"type": "completed", "status": "True"}]
    assert generation_job.evaluate_tasks([task]).exit_code == 83


@pytest.mark.parametrize("bad_code", [False, "0"])
def test_attempt_status_code_requires_json_integer(bad_code: object) -> None:
    task = _task()
    status = task["status"]
    assert isinstance(status, dict)
    attempt = status["lastAttemptResult"]
    assert isinstance(attempt, dict)
    attempt["status"] = {"code": bad_code}
    assert generation_job.evaluate_tasks([task]).exit_code == 83


@pytest.mark.parametrize("exit_code", range(1, 6))
def test_cli_failures_bypass_success_helper(
    monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    def forbidden(_task_value: Mapping[str, object]) -> generation_job.TaskDecision:
        raise AssertionError("success helper must not run for CLI failures")

    monkeypatch.setattr(generation_job, "_evaluate_success_gate", forbidden)
    task = _task(exit_code=exit_code)
    status = task["status"]
    assert isinstance(status, dict)
    status["conditions"] = [{"type": "Completed", "status": "False"}]
    attempt = status["lastAttemptResult"]
    assert isinstance(attempt, dict)
    attempt["status"] = {"code": 10}
    assert generation_job.evaluate_tasks([task]).exit_code == exit_code


def _marker(operation_id: str, *, exit_code: object = 0, reason: object = "ok") -> str:
    payload = {
        "operation_id": operation_id,
        "command": "delete-legacy",
        "space_key": "MFS",
        "generation_id": 1,
        "exit_code": exit_code,
        "reason": reason,
    }
    return generation_job.CLI_PREFIX + json.dumps(payload, separators=(",", ":"))


def test_marker_validation_count_content_and_derived_exit() -> None:
    operation_id = "00000000-0000-0000-0000-000000000001"
    valid = _marker(operation_id)
    marker, error = generation_job.validate_cli_markers(
        [valid], operation_id=operation_id, expected_exit=0
    )
    assert marker == generation_job.MarkerResult(0, "ok")
    assert error is None
    assert generation_job.validate_cli_markers([], operation_id=operation_id, expected_exit=0)[
        1
    ] == generation_job.TaskDecision(81, "result_marker_missing")
    assert generation_job.validate_cli_markers(
        [valid, valid], operation_id=operation_id, expected_exit=0
    )[1] == generation_job.TaskDecision(82, "result_marker_mismatch")
    for invalid in (
        _marker(operation_id, exit_code=False),
        _marker(operation_id, reason="unexpected_error"),
        _marker("00000000-0000-0000-0000-000000000002"),
        valid + "garbage",
    ):
        assert generation_job.validate_cli_markers(
            [invalid], operation_id=operation_id, expected_exit=0
        )[1] == generation_job.TaskDecision(82, "result_marker_mismatch")


@pytest.mark.parametrize(
    "invalid",
    [
        "DATABRIDGE_RESULT  {}",
        generation_job.CLI_PREFIX + "{not-json}",
        generation_job.CLI_PREFIX
        + json.dumps(
            {
                "operation_id": "00000000-0000-0000-0000-000000000001",
                "command": "delete-legacy",
                "space_key": "MFS",
                "generation_id": 1,
                "exit_code": 0,
            }
        ),
        _marker("00000000-0000-0000-0000-000000000001", exit_code=1),
        _marker("00000000-0000-0000-0000-000000000001", reason="unexpected_error"),
        generation_job.CLI_PREFIX
        + json.dumps(
            {
                "operation_id": "00000000-0000-0000-0000-000000000001",
                "command": "list",
                "space_key": "MFS",
                "generation_id": 1,
                "exit_code": 0,
                "reason": "ok",
            }
        ),
        _marker("00000000-0000-0000-0000-000000000002"),
    ],
)
def test_each_marker_contract_check_fails_closed(invalid: str) -> None:
    operation_id = "00000000-0000-0000-0000-000000000001"
    _, error = generation_job.validate_cli_markers(
        [invalid], operation_id=operation_id, expected_exit=0
    )
    assert error == generation_job.TaskDecision(82, "result_marker_mismatch")


def test_usage_error_marker_only_allows_known_commands() -> None:
    operation_id = "00000000-0000-0000-0000-000000000001"
    payload = {
        "operation_id": operation_id,
        "command": "invented-command",
        "space_key": None,
        "generation_id": None,
        "exit_code": 2,
        "reason": "cli_usage_error",
    }
    line = generation_job.CLI_PREFIX + json.dumps(payload)
    assert generation_job.validate_cli_markers([line], operation_id=operation_id, expected_exit=2)[
        1
    ] == generation_job.TaskDecision(82, "result_marker_mismatch")


def test_task_count_must_be_exactly_one() -> None:
    assert generation_job.evaluate_tasks([]).exit_code == 83
    assert generation_job.evaluate_tasks([_task(), _task()]).exit_code == 83


def test_execute_success_uses_two_step_task_lookup_and_one_execute() -> None:
    commands: list[list[str]] = []
    operation_id = ""

    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal operation_id
        argv = list(command)  # type: ignore[arg-type]
        commands.append(argv)
        joined = " ".join(argv)
        if " jobs execute " in f" {joined} ":
            env_arg = next(item for item in argv if item.startswith("--update-env-vars="))
            operation_id = env_arg.rsplit("=", 1)[1]
            return subprocess.CompletedProcess(argv, 0, "execution-1\n", "")
        if "executions describe execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"status": {"completionTime": "done"}}), ""
            )
        if "tasks list --execution execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"metadata": {"name": "task-1"}}]), ""
            )
        if "tasks describe task-1" in joined:
            return subprocess.CompletedProcess(argv, 0, json.dumps(_task()), "")
        if "logging read" in joined:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps([{"textPayload": _marker(operation_id)}]),
                "",
            )
        raise AssertionError(argv)

    decision, marker, observed_id = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["delete-legacy", "--space", "MFS"],
        run=run,
        sleep=lambda _seconds: None,
    )
    assert decision == generation_job.TaskDecision(0, "ok")
    assert marker == generation_job.MarkerResult(0, "ok")
    assert observed_id == operation_id
    assert sum(" jobs execute " in f" {' '.join(command)} " for command in commands) == 1
    task_describe = next(command for command in commands if "task-1" in command)
    assert "--execution" not in task_describe


@pytest.mark.parametrize(
    ("exit_code", "reason"),
    [(3, "structural_validation_failed"), (1, "unexpected_error")],
)
def test_measured_cli_failure_shapes_reach_real_marker_validation(
    exit_code: int, reason: str
) -> None:
    operation_id = ""

    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal operation_id
        argv = list(command)  # type: ignore[arg-type]
        joined = " ".join(argv)
        if " jobs execute " in f" {joined} ":
            env_arg = next(item for item in argv if item.startswith("--update-env-vars="))
            operation_id = env_arg.rsplit("=", 1)[1]
            return subprocess.CompletedProcess(argv, 0, "execution-1\n", "")
        if "executions describe execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"status": {"completionTime": "done"}}), ""
            )
        if "tasks list --execution execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"metadata": {"name": "task-1"}}]), ""
            )
        if "tasks describe task-1" in joined:
            task = _task(exit_code=exit_code)
            status = task["status"]
            assert isinstance(status, dict)
            status["conditions"] = [{"type": "Completed", "status": "False"}]
            attempt = status["lastAttemptResult"]
            assert isinstance(attempt, dict)
            attempt["status"] = {"code": 10}
            return subprocess.CompletedProcess(argv, 0, json.dumps(task), "")
        if "logging read" in joined:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [{"textPayload": _marker(operation_id, exit_code=exit_code, reason=reason)}]
                ),
                "",
            )
        raise AssertionError(argv)

    decision, marker, _ = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["delete-legacy"],
        run=run,
        sleep=lambda _seconds: None,
    )
    assert decision == generation_job.TaskDecision(exit_code, reason)
    assert marker == generation_job.MarkerResult(exit_code, reason)


def test_read_only_retry_exhaustion_is_wrapper_transport_error() -> None:
    describe_calls = 0

    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal describe_calls
        argv = list(command)  # type: ignore[arg-type]
        joined = " ".join(argv)
        if " jobs execute " in f" {joined} ":
            return subprocess.CompletedProcess(argv, 0, "execution-1\n", "")
        if "executions describe execution-1" in joined:
            describe_calls += 1
            return subprocess.CompletedProcess(argv, 1, "", "transport")
        raise AssertionError(argv)

    decision, marker, _ = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["list"],
        run=run,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert describe_calls == 3
    assert decision == generation_job.TaskDecision(85, "wrapper_transport_error")
    assert marker is None


def test_polling_wall_clock_deadline_returns_wrapper_timeout() -> None:
    now = 0.0

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        argv = list(command)  # type: ignore[arg-type]
        joined = " ".join(argv)
        if " jobs execute " in f" {joined} ":
            return subprocess.CompletedProcess(argv, 0, "execution-1\n", "")
        if "executions describe execution-1" in joined:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"status": {}}), "")
        raise AssertionError(argv)

    decision, marker, _ = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["list"],
        run=run,
        monotonic=monotonic,
        sleep=sleep,
        timeout_seconds=2.0,
    )
    assert decision == generation_job.TaskDecision(80, "wrapper_timeout")
    assert marker is None


def test_default_command_runner_passes_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    generation_job._run_command(["gcloud", "version"], 7.0)
    assert observed["timeout"] == 7.0


@pytest.mark.parametrize(
    ("candidates", "expected"),
    [
        (["execution-1"], generation_job.TaskDecision(0, "ok")),
        (
            ["execution-1", "execution-2"],
            generation_job.TaskDecision(87, "duplicate_execution"),
        ),
        (
            ["execution-1", None],
            generation_job.TaskDecision(85, "correlation_indeterminate"),
        ),
    ],
)
def test_failed_creation_correlation_one_duplicate_and_malformed(
    candidates: list[str | None], expected: generation_job.TaskDecision
) -> None:
    operation_id = ""

    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal operation_id
        argv = list(command)  # type: ignore[arg-type]
        joined = " ".join(argv)
        if " jobs execute " in f" {joined} ":
            env_arg = next(item for item in argv if item.startswith("--update-env-vars="))
            operation_id = env_arg.rsplit("=", 1)[1]
            return subprocess.CompletedProcess(argv, 1, "", "lost response")
        if "executions list" in joined:
            listed = [
                {"metadata": {"name": name}} if name is not None else {"metadata": {}}
                for name in candidates
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(listed), "")
        if "executions describe execution-" in joined:
            value = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "env": [
                                        {
                                            "name": "DATABRIDGE_OPERATION_ID",
                                            "value": operation_id,
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
            if len(candidates) == 1:
                value["status"] = {"completionTime": "done"}
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if "tasks list --execution execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"metadata": {"name": "task-1"}}]), ""
            )
        if "tasks describe task-1" in joined:
            return subprocess.CompletedProcess(argv, 0, json.dumps(_task()), "")
        if "logging read" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"textPayload": _marker(operation_id)}]), ""
            )
        raise AssertionError(argv)

    decision, _, _ = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["list"],
        run=run,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )
    assert decision == expected


@pytest.mark.parametrize("malformed", ["extra_task", "bad_item", "bad_description"])
def test_task_collection_malformed_shapes_are_83(malformed: str) -> None:
    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        argv = list(command)  # type: ignore[arg-type]
        joined = " ".join(argv)
        if " jobs execute " in f" {joined} ":
            return subprocess.CompletedProcess(argv, 0, "execution-1\n", "")
        if "executions describe execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"status": {"completionTime": "done"}}), ""
            )
        if "tasks list --execution execution-1" in joined:
            if malformed == "extra_task":
                value: object = [
                    {"metadata": {"name": "task-1"}},
                    {"metadata": {}},
                ]
            elif malformed == "bad_item":
                value = ["task-1"]
            else:
                value = [{"metadata": {"name": "task-1"}}]
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        if "tasks describe task-1" in joined and malformed == "bad_description":
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        raise AssertionError(argv)

    decision, marker, _ = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["list"],
        run=run,
        sleep=lambda _seconds: None,
    )
    assert decision == generation_job.TaskDecision(83, "task_result_unavailable")
    assert marker is None


def test_term_signal_wins_before_marker_lookup_in_full_execute_path() -> None:
    logging_called = False

    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal logging_called
        argv = list(command)  # type: ignore[arg-type]
        joined = " ".join(argv)
        if " jobs execute " in f" {joined} ":
            return subprocess.CompletedProcess(argv, 0, "execution-1\n", "")
        if "executions describe execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"status": {"completionTime": "done"}}), ""
            )
        if "tasks list --execution execution-1" in joined:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([{"metadata": {"name": "task-1"}}]), ""
            )
        if "tasks describe task-1" in joined:
            task = _task()
            status = task["status"]
            assert isinstance(status, dict)
            attempt = status["lastAttemptResult"]
            assert isinstance(attempt, dict)
            attempt["termSignal"] = 9
            return subprocess.CompletedProcess(argv, 0, json.dumps(task), "")
        if "logging read" in joined:
            logging_called = True
        raise AssertionError(argv)

    decision, marker, _ = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["activate"],
        run=run,
        sleep=lambda _seconds: None,
    )
    assert decision == generation_job.TaskDecision(84, "task_terminated_by_signal")
    assert marker is None
    assert logging_called is False


def test_failed_execute_is_never_retried_and_zero_correlation_is_indeterminate() -> None:
    execute_calls = 0

    def run(command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal execute_calls
        argv = list(command)  # type: ignore[arg-type]
        if "execute" in argv:
            execute_calls += 1
            return subprocess.CompletedProcess(argv, 1, "", "lost response")
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        raise AssertionError(argv)

    decision, marker, _ = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["activate"],
        run=run,
        sleep=lambda _seconds: None,
    )
    assert execute_calls == 1
    assert decision == generation_job.TaskDecision(85, "correlation_indeterminate")
    assert marker is None


def _wrapper_line(payload: dict[str, object]) -> str:
    return generation_job.WRAPPER_PREFIX + json.dumps(payload, separators=(",", ":"))


def test_wrapper_result_contract_is_fail_closed() -> None:
    operation_id = "00000000-0000-0000-0000-000000000001"
    valid = {
        "operation_id": operation_id,
        "wrapper_exit": 0,
        "wrapper_reason": "passthrough",
        "cli_exit": 0,
        "cli_reason": "ok",
    }
    assert generation_job.validate_wrapper_result([_wrapper_line(valid)], process_exit=0) == valid
    invalid_payloads = [
        {**valid, "wrapper_exit": False},
        {**valid, "cli_exit": False},
        {**valid, "wrapper_exit": 1},
        {**valid, "operation_id": None},
        {
            **valid,
            "wrapper_exit": 85,
            "wrapper_reason": "wrapper_transport_error",
            "cli_exit": 0,
            "cli_reason": "ok",
        },
    ]
    for payload in invalid_payloads:
        assert (
            generation_job.validate_wrapper_result(
                [_wrapper_line(payload)], process_exit=int(payload["wrapper_exit"])
            )
            is None
        )
    assert generation_job.validate_wrapper_result([], process_exit=0) is None
    assert (
        generation_job.validate_wrapper_result(
            [_wrapper_line(valid), _wrapper_line(valid)], process_exit=0
        )
        is None
    )
    assert (
        generation_job.validate_wrapper_result([_wrapper_line(valid) + "garbage"], process_exit=0)
        is None
    )
    assert (
        generation_job.validate_wrapper_result(
            [generation_job.WRAPPER_PREFIX + "{not-json}"], process_exit=0
        )
        is None
    )
    missing_field = dict(valid)
    missing_field.pop("cli_reason")
    assert (
        generation_job.validate_wrapper_result([_wrapper_line(missing_field)], process_exit=0)
        is None
    )
    bad_reason = {**valid, "wrapper_exit": 85, "wrapper_reason": "wrapper_timeout"}
    bad_reason["cli_exit"] = None
    bad_reason["cli_reason"] = None
    assert (
        generation_job.validate_wrapper_result([_wrapper_line(bad_reason)], process_exit=85) is None
    )


@pytest.mark.parametrize("exit_code", range(6))
def test_wrapper_result_passthrough_for_every_cli_exit(exit_code: int) -> None:
    operation_id = "00000000-0000-0000-0000-000000000001"
    reason = sorted(generation_job.REASONS[exit_code])[0]
    marker = generation_job.MarkerResult(exit_code, reason)
    decision = generation_job.TaskDecision(exit_code, reason)
    payload = generation_job._wrapper_payload(
        operation_id=operation_id, decision=decision, marker=marker
    )
    assert payload["wrapper_reason"] == "passthrough"
    assert payload["cli_exit"] == exit_code
    assert payload["cli_reason"] == reason
    assert (
        generation_job.validate_wrapper_result([_wrapper_line(payload)], process_exit=exit_code)
        == payload
    )


@pytest.mark.parametrize("exit_code", range(80, 88))
def test_wrapper_result_for_every_wrapper_exit(exit_code: int) -> None:
    operation_id = "00000000-0000-0000-0000-000000000001"
    reason = sorted(generation_job.WRAPPER_REASONS[exit_code])[0]
    decision = generation_job.TaskDecision(exit_code, reason)
    payload = generation_job._wrapper_payload(
        operation_id=operation_id, decision=decision, marker=None
    )
    assert payload["wrapper_reason"] == reason
    assert payload["cli_exit"] is None
    assert payload["cli_reason"] is None
    assert (
        generation_job.validate_wrapper_result([_wrapper_line(payload)], process_exit=exit_code)
        == payload
    )


def test_post_id_pre_request_failure_keeps_operation_id() -> None:
    def unavailable(_command: object, _timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gcloud")

    decision, marker, operation_id = generation_job.execute(
        project="p",
        region="r",
        job="j",
        job_args=["list", "--space", "MFS"],
        run=unavailable,
    )
    assert decision == generation_job.TaskDecision(85, "creation_confirmed_absent")
    assert marker is None
    assert generation_job._UUID.fullmatch(operation_id) is not None


def test_duplicate_valid_cli_markers_do_not_populate_wrapper_cli_fields() -> None:
    decision = generation_job.TaskDecision(82, "result_marker_mismatch")
    payload = generation_job._wrapper_payload(
        operation_id="00000000-0000-0000-0000-000000000001",
        decision=decision,
        marker=None,
    )
    assert payload["cli_exit"] is None
    assert payload["cli_reason"] is None


def test_pre_id_wrapper_failure_has_null_operation_and_confirmed_absent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert generation_job.main([]) == 85
    line = capsys.readouterr().out.strip()
    payload = generation_job.validate_wrapper_result([line], process_exit=85)
    assert payload is not None
    assert payload["operation_id"] is None
    assert payload["wrapper_reason"] == "creation_confirmed_absent"
