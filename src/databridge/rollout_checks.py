"""Pure, fail-closed checks for the generation rollout runbook."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TypeGuard

WRAPPER_PREFIX = "DATABRIDGE_WRAPPER_RESULT "
_DIGEST_IMAGE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")


class RolloutCheckError(RuntimeError):
    """Raised when a rollout result cannot be determined safely."""


def _json_int(value: object) -> TypeGuard[int]:
    return type(value) is int


def _mapping(
    value: object, *, label: str, problems: list[str]
) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        problems.append(f"{label} is missing or is not an object")
        return None
    return value


def _sequence(value: object) -> Sequence[object] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def check_dsn_secret_ref(env: Sequence[Mapping[str, object]]) -> list[str]:
    """Require exactly one DSN environment entry backed by a usable secret reference."""
    problems: list[str] = []
    entries = [item for item in env if item.get("name") == "DATABRIDGE_DSN"]
    if len(entries) != 1:
        return [f"expected exactly one DATABRIDGE_DSN entry, found {len(entries)}"]

    entry = entries[0]
    if "value" in entry:
        problems.append("DATABRIDGE_DSN must not contain a literal value")

    value_from = _mapping(
        entry.get("valueFrom"), label="DATABRIDGE_DSN valueFrom", problems=problems
    )
    if value_from is None:
        return problems
    secret_ref = _mapping(
        value_from.get("secretKeyRef"),
        label="DATABRIDGE_DSN secretKeyRef",
        problems=problems,
    )
    if secret_ref is None:
        return problems
    if not isinstance(secret_ref.get("name"), str) or not secret_ref["name"]:
        problems.append("DATABRIDGE_DSN secretKeyRef name is missing")
    if not isinstance(secret_ref.get("key"), str) or not secret_ref["key"]:
        problems.append("DATABRIDGE_DSN secretKeyRef key is missing")
    return problems


def check_generation_job(job: Mapping[str, object]) -> list[str]:
    """Check the Cloud Run generation job invariants used by the wrapper."""
    problems: list[str] = []
    spec = _mapping(job.get("spec"), label="job spec", problems=problems)
    template = (
        _mapping(spec.get("template"), label="job execution template", problems=problems)
        if spec is not None
        else None
    )
    execution = (
        _mapping(template.get("spec"), label="job execution spec", problems=problems)
        if template is not None
        else None
    )
    if execution is None:
        return problems

    task_count = execution.get("taskCount")
    if not _json_int(task_count) or task_count != 1:
        problems.append(f"taskCount={task_count!r}, expected the explicit integer 1")

    if "parallelism" in execution:
        parallelism = execution["parallelism"]
        if not _json_int(parallelism) or parallelism != 1:
            problems.append(
                f"parallelism={parallelism!r}, expected omission or the integer 1"
            )

    task_template = _mapping(
        execution.get("template"), label="task template", problems=problems
    )
    task = (
        _mapping(task_template.get("spec"), label="task spec", problems=problems)
        if task_template is not None
        else None
    )
    if task is None:
        return problems

    max_retries = task.get("maxRetries")
    if not _json_int(max_retries) or max_retries != 0:
        problems.append(f"maxRetries={max_retries!r}, expected the explicit integer 0")

    timeout = task.get("timeoutSeconds")
    try:
        timeout_seconds = int(timeout) if isinstance(timeout, str) else None
    except ValueError:
        timeout_seconds = None
    if timeout_seconds != 3600:
        problems.append(f"timeoutSeconds={timeout!r}, expected the string '3600'")

    containers = _sequence(task.get("containers"))
    if containers is None or len(containers) != 1:
        problems.append("task containers must contain exactly one object")
        return problems
    container = _mapping(containers[0], label="task container", problems=problems)
    if container is None:
        return problems
    raw_env = _sequence(container.get("env", []))
    if raw_env is None:
        problems.append("task container env is not a list")
        return problems
    env: list[Mapping[str, object]] = []
    for index, item in enumerate(raw_env):
        if not isinstance(item, Mapping):
            problems.append(f"task container env[{index}] is not an object")
        else:
            env.append(item)
    problems.extend(check_dsn_secret_ref(env))
    return problems


#: Every job that must be carrying the rollout image before traffic moves. A job left
#: behind writes with the old schema assumptions, so the set is fixed here rather than
#: supplied by the caller: checking a subset and reporting success is the failure mode.
ROLLOUT_JOBS: tuple[str, ...] = (
    "databridge-migrate",
    "databridge-ingest",
    "databridge-confluence-ingest",
    "databridge-generation",
)


def check_images(*, expected: str, service: str, jobs: Mapping[str, str]) -> list[str]:
    """Check that the service and every rollout job use one independently chosen digest."""
    problems: list[str] = []
    if _DIGEST_IMAGE.fullmatch(expected) is None:
        problems.append("expected image is not pinned to a canonical sha256 digest")
    if service != expected:
        problems.append("service image does not match the expected image")
    missing = [name for name in ROLLOUT_JOBS if name not in jobs]
    if missing:
        problems.append(f"no image was read for {', '.join(missing)}")
    for name, image in sorted(jobs.items()):
        if image != expected:
            problems.append(f"job {name} image does not match the expected image")
    return problems


def check_report(
    report: Mapping[str, object], *, expected_generation: int
) -> list[str]:
    """Check the post-cutover generation report before resuming writers."""
    problems: list[str] = []
    if not _json_int(expected_generation) or expected_generation <= 0:
        problems.append("expected generation must be a positive JSON integer")

    legacy = report.get("legacy_null_generation_chunks")
    if not _json_int(legacy):
        problems.append("legacy_null_generation_chunks is not a JSON integer")
    elif legacy != 0:
        problems.append(f"legacy_null_generation_chunks={legacy}, expected 0")

    raw_generations = _sequence(report.get("generations"))
    if raw_generations is None:
        problems.append("generations is missing or is not a list")
        return problems
    generations: list[Mapping[str, object]] = []
    for index, item in enumerate(raw_generations):
        if not isinstance(item, Mapping):
            problems.append(f"generations[{index}] is not an object")
        else:
            generations.append(item)
    active = [item for item in generations if item.get("state") == "active"]
    if len(active) != 1:
        problems.append(f"expected exactly one active generation, found {len(active)}")
        return problems

    generation_id = active[0].get("generation_id")
    if not _json_int(generation_id):
        problems.append("active generation_id is not a JSON integer")
    elif generation_id != expected_generation:
        problems.append(
            f"active generation_id={generation_id}, expected {expected_generation}"
        )
    chunk_count = active[0].get("chunk_count")
    if not _json_int(chunk_count):
        problems.append("active chunk_count is not a JSON integer")
    elif chunk_count <= 0:
        problems.append("active generation holds no chunks")
    return problems


def _execution_env(execution: Mapping[str, object], *, index: int) -> tuple[str, Sequence[object]]:
    try:
        metadata = execution["metadata"]
        if not isinstance(metadata, Mapping):
            raise TypeError
        name = metadata["name"]
        if not isinstance(name, str) or not name:
            raise TypeError
        spec = execution["spec"]
        if not isinstance(spec, Mapping):
            raise TypeError
        template = spec["template"]
        if not isinstance(template, Mapping):
            raise TypeError
        template_spec = template["spec"]
        if not isinstance(template_spec, Mapping):
            raise TypeError
        containers = _sequence(template_spec["containers"])
        if containers is None or len(containers) != 1 or not isinstance(containers[0], Mapping):
            raise TypeError
        env = _sequence(containers[0].get("env", []))
        if env is None or any(not isinstance(item, Mapping) for item in env):
            raise TypeError
    except (KeyError, TypeError) as exc:
        raise RolloutCheckError(f"execution candidate {index} cannot be inspected") from exc
    return name, env


def correlate_execution(
    executions: Sequence[Mapping[str, object]], *, operation_id: str
) -> str:
    """Return the only execution carrying an operation id, or fail closed."""
    if not isinstance(operation_id, str) or not operation_id:
        raise RolloutCheckError("operation_id must be a non-empty string")
    matches: list[str] = []
    for index, execution in enumerate(executions):
        name, env = _execution_env(execution, index=index)
        if any(
            isinstance(item, Mapping)
            and item.get("name") == "DATABRIDGE_OPERATION_ID"
            and item.get("value") == operation_id
            for item in env
        ):
            matches.append(name)
    if len(matches) != 1:
        raise RolloutCheckError(
            f"expected exactly one execution for the operation id, found {len(matches)}"
        )
    return matches[0]


def read_generation_id(lines: Sequence[str]) -> int:
    """Read a generation id from one successful wrapper result line."""
    candidates = [line for line in lines if line.startswith(WRAPPER_PREFIX)]
    if len(candidates) != 1:
        raise RolloutCheckError(
            f"expected exactly one wrapper result line, found {len(candidates)}"
        )
    try:
        payload = json.loads(candidates[0][len(WRAPPER_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise RolloutCheckError("wrapper result is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise RolloutCheckError("wrapper result is not a JSON object")
    if not _json_int(payload.get("wrapper_exit")) or payload["wrapper_exit"] != 0:
        raise RolloutCheckError("wrapper result is not a wrapper success")
    if not _json_int(payload.get("cli_exit")) or payload["cli_exit"] != 0:
        raise RolloutCheckError("wrapper result is not a CLI success")
    generation_id = payload.get("cli_generation_id")
    if not _json_int(generation_id) or generation_id <= 0:
        raise RolloutCheckError("cli_generation_id is not a positive JSON integer")
    return generation_id


def check_env_absent(env: Sequence[Mapping[str, object]], name: str) -> list[str]:
    """Report every occurrence of an environment variable that must be absent."""
    count = sum(item.get("name") == name for item in env)
    if count:
        return [f"environment variable {name} must be absent (found {count})"]
    return []
