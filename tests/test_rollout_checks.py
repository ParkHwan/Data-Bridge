"""Regression tests for rollout checks extracted from the reviewed runbook."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from databridge.rollout_checks import (
    ROLLOUT_JOBS,
    WRAPPER_PREFIX,
    RolloutCheckError,
    check_dsn_secret_ref,
    check_env_absent,
    check_generation_job,
    check_images,
    check_ingest_scope,
    check_report,
    check_rollback_is_safe,
    check_strict_mode,
    correlate_execution,
    extract_report_body,
    read_generation_id,
    read_operation_id,
    read_serving_revision,
)

_MISSING = object()
IMAGE = "us-central1-docker.pkg.dev/project/repository/databridge@sha256:" + "a" * 64


def _secret_env() -> dict[str, object]:
    return {
        "name": "DATABRIDGE_DSN",
        "valueFrom": {"secretKeyRef": {"name": "DATABRIDGE_DSN", "key": "latest"}},
    }


def _job(
    *,
    task_count: object = 1,
    parallelism: object = _MISSING,
    max_retries: object = 0,
    timeout: object = "3600",
    env: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    execution: dict[str, object] = {
        "taskCount": task_count,
        "template": {
            "spec": {
                "maxRetries": max_retries,
                "timeoutSeconds": timeout,
                "containers": [{"env": [_secret_env()] if env is None else env}],
            }
        },
    }
    if parallelism is not _MISSING:
        execution["parallelism"] = parallelism
    return {"spec": {"template": {"spec": execution}}}


def _without(job: dict[str, object], key: str) -> dict[str, object]:
    changed = deepcopy(job)
    spec = changed["spec"]
    assert isinstance(spec, dict)
    template = spec["template"]
    assert isinstance(template, dict)
    execution = template["spec"]
    assert isinstance(execution, dict)
    if key in execution:
        execution.pop(key)
    else:
        task_template = execution["template"]
        assert isinstance(task_template, dict)
        task = task_template["spec"]
        assert isinstance(task, dict)
        task.pop(key)
    return changed


def test_generation_job_accepts_measured_shape() -> None:
    assert check_generation_job(_job()) == []


@pytest.mark.parametrize(
    ("job", "field"),
    [
        (_without(_job(), "maxRetries"), "maxRetries"),
        (_job(max_retries=3), "maxRetries"),
        (_without(_job(), "taskCount"), "taskCount"),
        (_job(task_count=2), "taskCount"),
        (_job(timeout="600"), "timeoutSeconds"),
    ],
)
def test_generation_job_rejects_runbook_regressions(
    job: dict[str, object], field: str
) -> None:
    assert any(field in problem for problem in check_generation_job(job))


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.parametrize("field", ["taskCount", "maxRetries", "parallelism"])
def test_generation_job_rejects_boolean_integer_fields(field: str, value: bool) -> None:
    if field == "taskCount":
        job = _job(task_count=value)
    elif field == "maxRetries":
        job = _job(max_retries=value)
    else:
        job = _job(parallelism=value)
    assert any(field in problem for problem in check_generation_job(job))


def test_generation_job_accepts_explicit_parallelism_one() -> None:
    assert check_generation_job(_job(parallelism=1)) == []


@pytest.mark.parametrize(
    "env",
    [
        [],
        [{"name": "DATABRIDGE_DSN", "value": "redacted-literal"}],
        [{"name": "DATABRIDGE_DSN"}],
        [{"name": "DATABRIDGE_DSN", "value": None}],
        [_secret_env(), _secret_env()],
        [{"name": "DATABRIDGE_DSN", "valueFrom": {"secretKeyRef": {"name": "x"}}}],
    ],
)
def test_dsn_rejects_missing_literal_bare_null_duplicate_and_incomplete_refs(
    env: list[dict[str, object]],
) -> None:
    assert check_dsn_secret_ref(env)


def test_dsn_accepts_one_complete_secret_reference() -> None:
    assert check_dsn_secret_ref([_secret_env()]) == []


def _report(
    *,
    legacy: object = 0,
    active: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "legacy_null_generation_chunks": legacy,
        "generations": (
            [{"generation_id": 7, "state": "active", "chunk_count": 12}]
            if active is None
            else active
        ),
    }


def test_report_accepts_actual_output_shape() -> None:
    assert check_report(_report(), expected_generation=7) == []


def test_report_rejects_boolean_legacy_count() -> None:
    assert "not a JSON integer" in " ".join(
        check_report(_report(legacy=False), expected_generation=7)
    )


def test_report_rejects_boolean_chunk_count() -> None:
    report = _report(active=[{"generation_id": 7, "state": "active", "chunk_count": True}])
    assert "chunk_count" in " ".join(check_report(report, expected_generation=7))


@pytest.mark.parametrize(
    "active",
    [
        [],
        [
            {"generation_id": 7, "state": "active", "chunk_count": 1},
            {"generation_id": 8, "state": "active", "chunk_count": 1},
        ],
    ],
)
def test_report_requires_exactly_one_active_generation(
    active: list[dict[str, object]],
) -> None:
    assert "exactly one active" in " ".join(
        check_report(_report(active=active), expected_generation=7)
    )


def test_report_rejects_active_generation_mismatch() -> None:
    assert "expected 8" in " ".join(check_report(_report(), expected_generation=8))


def _execution(name: str, operation_id: str | None) -> dict[str, object]:
    env: list[dict[str, object]] = []
    if operation_id is not None:
        env.append({"name": "DATABRIDGE_OPERATION_ID", "value": operation_id})
    return {
        "metadata": {"name": name},
        "spec": {"template": {"spec": {"containers": [{"env": env}]}}},
    }


def test_correlate_execution_returns_the_only_match() -> None:
    assert (
        correlate_execution(
            [_execution("other", "other-id"), _execution("wanted", "op-id")],
            operation_id="op-id",
        )
        == "wanted"
    )


@pytest.mark.parametrize(
    "executions",
    [
        [],
        [_execution("other", "other-id")],
        [_execution("first", "op-id"), _execution("second", "op-id")],
    ],
)
def test_correlate_execution_rejects_zero_and_multiple_matches(
    executions: list[dict[str, object]],
) -> None:
    with pytest.raises(RolloutCheckError):
        correlate_execution(executions, operation_id="op-id")


def test_correlate_execution_rejects_any_unreadable_candidate() -> None:
    with pytest.raises(RolloutCheckError, match="cannot be inspected"):
        correlate_execution(
            [_execution("wanted", "op-id"), {"metadata": {"name": "unreadable"}}],
            operation_id="op-id",
        )


def _wrapper_line(
    *,
    wrapper_exit: object = 0,
    cli_exit: object = 0,
    generation_id: object = 7,
    operation_id: object = "00000000-0000-4000-8000-000000000001",
) -> str:
    return WRAPPER_PREFIX + json.dumps(
        {
            "operation_id": operation_id,
            "wrapper_exit": wrapper_exit,
            "wrapper_reason": "passthrough",
            "cli_exit": cli_exit,
            "cli_reason": "ok",
            "cli_generation_id": generation_id,
        }
    )


def test_read_generation_id_returns_positive_integer() -> None:
    assert read_generation_id([_wrapper_line()]) == 7


@pytest.mark.parametrize("lines", [[], [_wrapper_line(), _wrapper_line()]])
def test_read_generation_id_requires_exactly_one_result(lines: list[str]) -> None:
    with pytest.raises(RolloutCheckError):
        read_generation_id(lines)


@pytest.mark.parametrize(
    "line",
    [
        _wrapper_line(wrapper_exit=1),
        _wrapper_line(cli_exit=1),
        _wrapper_line(generation_id=True),
        _wrapper_line(generation_id=0),
        _wrapper_line(generation_id=-1),
        _wrapper_line(generation_id="1"),
        _wrapper_line(generation_id=None),
    ],
)
def test_read_generation_id_rejects_failed_and_ambiguous_results(line: str) -> None:
    with pytest.raises(RolloutCheckError):
        read_generation_id([line])


def test_images_accept_all_matching_digest_pinned_resources() -> None:
    assert check_images(
        expected=IMAGE, service=IMAGE, jobs=dict.fromkeys(ROLLOUT_JOBS, IMAGE)
    ) == []


def test_images_reject_a_subset_of_the_required_jobs() -> None:
    """A partial set that reports OK is the drift this check exists to catch."""
    assert check_images(expected=IMAGE, service=IMAGE, jobs={}) != []
    for name in ROLLOUT_JOBS:
        partial = dict.fromkeys(ROLLOUT_JOBS, IMAGE)
        del partial[name]
        problems = check_images(expected=IMAGE, service=IMAGE, jobs=partial)
        assert any(name in problem for problem in problems), name


@pytest.mark.parametrize(
    ("expected", "service", "jobs", "message"),
    [
        (IMAGE, "old-image", {"job": IMAGE}, "service"),
        (IMAGE, IMAGE, {"job": "old-image"}, "job job"),
        ("image:latest", "image:latest", {"job": "image:latest"}, "canonical sha256"),
    ],
)
def test_images_reject_service_job_and_unpinned_mismatches(
    expected: str, service: str, jobs: dict[str, str], message: str
) -> None:
    assert message in " ".join(check_images(expected=expected, service=service, jobs=jobs))


def test_env_absent_reports_presence_without_exposing_values() -> None:
    problems = check_env_absent([{"name": "FORBIDDEN", "value": "secret"}], "FORBIDDEN")
    assert problems
    assert "secret" not in " ".join(problems)


def _env(**values: str) -> list[dict[str, object]]:
    return [{"name": key, "value": value} for key, value in values.items()]


def test_ingest_scope_rejects_a_different_corpus() -> None:
    """setup_cicd tells the provisioner to use a dedicated key, so this cannot be assumed."""
    good = _env(SPACE_KEY="MFS", FOLDER_ID="98380")
    assert check_ingest_scope(good, space_key="MFS", folder_id="98380") == []
    assert check_ingest_scope(
        _env(SPACE_KEY="CONF_DEMO", FOLDER_ID="98380"), space_key="MFS", folder_id="98380"
    ) != []
    assert check_ingest_scope(
        _env(SPACE_KEY="MFS", FOLDER_ID="1"), space_key="MFS", folder_id="98380"
    ) != []
    assert check_ingest_scope([], space_key="MFS", folder_id="98380") != []


def test_ingest_scope_rejects_a_permanent_generation_override() -> None:
    """An execution-time override is fine; a permanent one retargets every future run."""
    env = _env(SPACE_KEY="MFS", FOLDER_ID="98380", DATABRIDGE_GENERATION_ID="7")
    assert check_ingest_scope(env, space_key="MFS", folder_id="98380") != []


def test_strict_mode_requires_the_service_and_every_job() -> None:
    strict = _env(DATABRIDGE_PROFILE_MODE="strict")
    observe = _env(DATABRIDGE_PROFILE_MODE="observe")
    assert check_strict_mode(
        service_env=strict, job_envs=dict.fromkeys(ROLLOUT_JOBS, strict)
    ) == []
    # A job left on observe keeps writing under the condition strict exists to reject.
    for name in ROLLOUT_JOBS:
        envs: dict[str, list[dict[str, object]]] = dict.fromkeys(ROLLOUT_JOBS, strict)
        envs[name] = observe
        problems = check_strict_mode(service_env=strict, job_envs=envs)
        assert any(name in problem for problem in problems), name
    assert check_strict_mode(
        service_env=observe, job_envs=dict.fromkeys(ROLLOUT_JOBS, strict)
    ) != []


def test_strict_mode_rejects_an_unread_job() -> None:
    """A job whose environment was never read must not count as compliant."""
    strict = _env(DATABRIDGE_PROFILE_MODE="strict")
    assert check_strict_mode(service_env=strict, job_envs={}) != []
    for name in ROLLOUT_JOBS:
        partial: dict[str, list[dict[str, object]]] = dict.fromkeys(ROLLOUT_JOBS, strict)
        del partial[name]
        assert any(
            name in problem
            for problem in check_strict_mode(service_env=strict, job_envs=partial)
        ), name


def test_strict_mode_rejects_a_permanent_generation_override() -> None:
    strict = _env(DATABRIDGE_PROFILE_MODE="strict")
    leaked = _env(DATABRIDGE_PROFILE_MODE="strict", DATABRIDGE_GENERATION_ID="7")
    assert check_strict_mode(service_env=leaked, job_envs=dict.fromkeys(ROLLOUT_JOBS, strict)) != []
    envs: dict[str, list[dict[str, object]]] = dict.fromkeys(ROLLOUT_JOBS, strict)
    envs["databridge-generation"] = leaked
    assert check_strict_mode(service_env=strict, job_envs=envs) != []


def test_operation_id_refuses_anything_ambiguous() -> None:
    line = _wrapper_line()
    assert read_operation_id([line]) == "00000000-0000-4000-8000-000000000001"
    ambiguous = (
        [],
        [line, line],
        [_wrapper_line(operation_id=None)],
        [_wrapper_line(operation_id="")],
    )
    for bad in ambiguous:
        with pytest.raises(RolloutCheckError):
            read_operation_id(bad)


def test_duplicate_env_names_fail_regardless_of_order() -> None:
    """The last entry must not decide whether a rollout is safe.

    A dict comprehension let SPACE_KEY=WRONG followed by SPACE_KEY=MFS read as correct,
    and observe followed by strict read as strict.
    """
    both_orders = (
        (("SPACE_KEY", "WRONG"), ("SPACE_KEY", "MFS")),
        (("SPACE_KEY", "MFS"), ("SPACE_KEY", "WRONG")),
    )
    for pairs in both_orders:
        env = [{"name": name, "value": value} for name, value in pairs]
        env.append({"name": "FOLDER_ID", "value": "98380"})
        problems = check_ingest_scope(env, space_key="MFS", folder_id="98380")
        assert any("appears 2 times" in problem for problem in problems), pairs

    for modes in (("observe", "strict"), ("strict", "observe")):
        env = [{"name": "DATABRIDGE_PROFILE_MODE", "value": mode} for mode in modes]
        problems = check_strict_mode(
            service_env=env, job_envs=dict.fromkeys(ROLLOUT_JOBS, env)
        )
        assert any("appears 2 times" in problem for problem in problems), modes


def test_report_body_is_taken_from_a_real_command_capture() -> None:
    """The capture holds the report and the CLI marker, so it is not one JSON document."""
    report = {
        "space_key": "MFS",
        "legacy_null_generation_chunks": 0,
        "generations": [{"generation_id": 7, "state": "active", "chunk_count": 5}],
    }
    marker = 'DATABRIDGE_RESULT {"command":"report","exit_code":0,"reason":"ok"}'
    lines = [json.dumps(report), marker]
    assert extract_report_body(lines) == report
    assert extract_report_body(["", *lines, ""]) == report
    # Two reports in one capture means we cannot tell which run this is.
    with pytest.raises(RolloutCheckError):
        extract_report_body([json.dumps(report), json.dumps(report), marker])
    with pytest.raises(RolloutCheckError):
        extract_report_body([marker])


def test_rollback_is_refused_while_any_generation_holds_chunks() -> None:
    """The old reader is space-only, so one building row makes a rollback mix generations."""
    empty = {"generations": [{"generation_id": 1, "chunk_count": 0}]}
    assert check_rollback_is_safe(inventory={"total_chunks": 0}, report=empty) == []
    assert check_rollback_is_safe(inventory={"total_chunks": 3}, report=empty) != []
    assert check_rollback_is_safe(
        inventory={"total_chunks": 0},
        report={"generations": [{"generation_id": 1, "chunk_count": 12}]},
    ) != []
    # Booleans satisfy != 0 and <= 0 comparisons, so they must fail on type.
    for bad in (True, False, "0", None):
        assert check_rollback_is_safe(inventory={"total_chunks": bad}, report=empty) != []
        assert check_rollback_is_safe(
            inventory={"total_chunks": 0},
            report={"generations": [{"generation_id": 1, "chunk_count": bad}]},
        ) != []
    assert check_rollback_is_safe(inventory={"total_chunks": 0}, report={}) != []


def test_serving_revision_refuses_a_split_or_tagged_allocation() -> None:
    """traffic[0] would name a tag target or half a split as "the serving revision"."""
    assert read_serving_revision([{"revisionName": "rev-a", "percent": 100}]) == "rev-a"
    ambiguous = (
        [],
        [{"revisionName": "rev-a", "percent": 50}, {"revisionName": "rev-b", "percent": 50}],
        [{"revisionName": "rev-a", "percent": 100}, {"revisionName": "rev-b", "tag": "x"}],
        [{"percent": 100}],
        [{"revisionName": "", "percent": 100}],
        [{"revisionName": "rev-a", "percent": "100"}],
    )
    for traffic in ambiguous:
        with pytest.raises(RolloutCheckError):
            read_serving_revision(traffic)
