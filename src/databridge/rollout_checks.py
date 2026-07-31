"""Pure, fail-closed checks for the generation rollout runbook."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
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


def _one_wrapper_result(lines: Sequence[str]) -> Mapping[str, object]:
    """Return the single wrapper result line, refusing anything ambiguous."""
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
    return payload


def read_generation_id(lines: Sequence[str]) -> int:
    """Read a generation id from one successful wrapper result line."""
    payload = _one_wrapper_result(lines)
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

def _env_lookup(
    env: Sequence[Mapping[str, object]], names: Sequence[str], problems: list[str]
) -> dict[str, object]:
    """Resolve the named variables, refusing any that appears more than once.

    A dict comprehension would let the last entry win, so an env carrying both
    SPACE_KEY=WRONG and SPACE_KEY=MFS — or observe followed by strict — would be read
    as correct. Order must not decide whether a rollout is safe.
    """
    values: dict[str, object] = {}
    for name in names:
        matches = [
            item
            for item in env
            if isinstance(item, Mapping) and item.get("name") == name
        ]
        if len(matches) > 1:
            problems.append(f"{name} appears {len(matches)} times; expected at most once")
            continue
        if matches:
            values[name] = matches[0].get("value")
    return values


def check_ingest_scope(
    env: Sequence[Mapping[str, object]], *, space_key: str, folder_id: str
) -> list[str]:
    """Check the Confluence ingest job targets the space being migrated.

    setup_cicd.sh tells the provisioner to use a dedicated corpus key, so the configured
    space is not necessarily the one being rolled out. A mismatch would ingest the wrong
    corpus into a fresh generation.
    """
    problems: list[str] = []
    values = _env_lookup(
        env, ("SPACE_KEY", "FOLDER_ID", "DATABRIDGE_GENERATION_ID"), problems
    )
    if values.get("SPACE_KEY") != space_key:
        problems.append(f"SPACE_KEY={values.get('SPACE_KEY')!r}, expected {space_key!r}")
    if values.get("FOLDER_ID") != folder_id:
        problems.append(f"FOLDER_ID={values.get('FOLDER_ID')!r}, expected {folder_id!r}")
    if "DATABRIDGE_GENERATION_ID" in values:
        problems.append("the job carries a permanent DATABRIDGE_GENERATION_ID")
    return problems


def check_strict_mode(
    *,
    service_env: Sequence[Mapping[str, object]],
    job_envs: Mapping[str, Sequence[Mapping[str, object]]],
) -> list[str]:
    """Require strict profile mode everywhere, with no permanent generation override.

    A job left on observe keeps writing under the condition strict exists to reject, so
    the whole set has to move together.
    """
    problems: list[str] = []

    def _one(label: str, env: Sequence[Mapping[str, object]]) -> None:
        local: list[str] = []
        values = _env_lookup(
            env, ("DATABRIDGE_PROFILE_MODE", "DATABRIDGE_GENERATION_ID"), local
        )
        problems.extend(f"{label} {problem}" for problem in local)
        mode = values.get("DATABRIDGE_PROFILE_MODE")
        if mode != "strict":
            problems.append(f"{label} DATABRIDGE_PROFILE_MODE={mode!r}, expected 'strict'")
        if "DATABRIDGE_GENERATION_ID" in values:
            problems.append(f"{label} carries a permanent DATABRIDGE_GENERATION_ID")

    _one("service", service_env)
    missing = [name for name in ROLLOUT_JOBS if name not in job_envs]
    if missing:
        problems.append(f"no environment was read for {', '.join(missing)}")
    for name, env in sorted(job_envs.items()):
        _one(f"job {name}", env)
    return problems


def read_operation_id(lines: Sequence[str]) -> str:
    """Return the operation id from exactly one valid wrapper result line."""
    payload = _one_wrapper_result(lines)
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise RolloutCheckError("the wrapper reported no operation id to correlate")
    return operation_id


CLI_PREFIX = "DATABRIDGE_RESULT "


def extract_report_body(lines: Sequence[str]) -> Mapping[str, object]:
    """Return the one report object from a command's captured stdout.

    The log carries the report and the CLI result marker, so the whole capture is not a
    JSON document. Picking the first or last line instead would depend on log ordering.
    Take the lines that parse as JSON objects and are not the marker, and require exactly
    one — two would mean we cannot tell which run we are looking at.
    """
    candidates: list[Mapping[str, object]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("{") or stripped.startswith(CLI_PREFIX):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            candidates.append(parsed)
    if len(candidates) != 1:
        raise RolloutCheckError(
            f"expected exactly one report object in the output, found {len(candidates)}"
        )
    return candidates[0]


def check_rollback_is_safe(
    *, inventory: Mapping[str, object], report: Mapping[str, object]
) -> list[str]:
    """Check that no generation holds chunks, so the old reader cannot mix rows.

    Rolling traffic back after the clean ingest started is only safe while the space
    holds no non-null-generation chunk. Reading the two outputs by eye is how that gets
    decided wrongly, and a wrong reading here makes the old space-only reader return
    legacy and building rows together.
    """
    problems: list[str] = []

    total = inventory.get("total_chunks")
    if not _json_int(total):
        problems.append("inventory total_chunks is not a JSON integer")
    elif total != 0:
        problems.append(f"inventory total_chunks={total}, expected 0")

    raw_generations = _sequence(report.get("generations"))
    if raw_generations is None:
        problems.append("report generations is missing or is not a list")
        return problems
    for index, item in enumerate(raw_generations):
        if not isinstance(item, Mapping):
            problems.append(f"report generations[{index}] is not an object")
            continue
        count = item.get("chunk_count")
        if not _json_int(count):
            problems.append(f"generations[{index}] chunk_count is not a JSON integer")
        elif count != 0:
            problems.append(
                f"generation {item.get('generation_id')!r} holds {count} chunks; "
                "rolling back would mix generations"
            )
    return problems


def read_serving_revision(traffic: Sequence[Mapping[str, object]]) -> str:
    """Return the one revision serving all traffic, refusing anything ambiguous.

    Taking ``traffic[0]`` would name a tag target or one half of a split as "the serving
    revision", and the rollback would then send traffic somewhere that was never serving.
    """
    serving = [
        item
        for item in traffic
        if isinstance(item, Mapping) and item.get("percent") == 100
    ]
    if len(serving) != 1:
        raise RolloutCheckError(
            f"expected exactly one revision at 100 percent, found {len(serving)}"
        )
    name = serving[0].get("revisionName")
    if not isinstance(name, str) or not name:
        raise RolloutCheckError("the serving traffic target has no revision name")
    if len(traffic) != 1:
        raise RolloutCheckError(
            "traffic is split or tagged; record the whole allocation before moving it"
        )
    return name


def check_writers_quiesced(executions: Sequence[Mapping[str, object]]) -> list[str]:
    """Require positive evidence that every ingest execution has finished.

    Whatever inventory and report say is only true while nothing writes, so this has to
    establish that nothing *will* write either. An absent runningCount does not do that:
    a pending execution — created, tasks not yet started — carries the same empty status
    as a finished one, and would go on to write chunks after the rollback was approved.

    Measured on a finished execution: status carries completionTime and a Completed
    condition whose status is the string "True". Both are required here.
    """
    problems: list[str] = []
    for index, item in enumerate(executions):
        if not isinstance(item, Mapping):
            problems.append(f"execution[{index}] is not an object")
            continue
        metadata = item.get("metadata")
        label = (
            metadata.get("name") if isinstance(metadata, Mapping) else None
        ) or f"execution[{index}]"

        status = item.get("status")
        if not isinstance(status, Mapping):
            problems.append(f"{label} has no readable status")
            continue

        running = status.get("runningCount", 0)
        if not _json_int(running):
            problems.append(f"{label} runningCount is not a JSON integer")
        elif running != 0:
            problems.append(f"{label} is still running ({running} task(s))")

        if not status.get("completionTime"):
            problems.append(
                f"{label} has no completionTime; it has not finished and may still write"
            )
        conditions = _sequence(status.get("conditions")) or ()
        completed = [
            item
            for item in conditions
            if isinstance(item, Mapping) and item.get("type") == "Completed"
        ]
        if len(completed) != 1 or completed[0].get("status") not in {"True", "False"}:
            problems.append(f"{label} has no single readable Completed condition")
    return problems


def check_recovery_point(
    backups: Sequence[Mapping[str, object]],
    *,
    now: datetime,
    max_age_hours: int,
) -> list[str]:
    """Require a restore point that actually exists and is recent enough.

    "PITR is enabled" is not the same as being able to recover: this instance had the
    flag unset, no automated backups and zero backup runs, so get-latest-recovery-time
    reported that no usable recovery point existed at all. What matters before an
    irreversible delete is a concrete artefact, so this asks for a SUCCESSFUL backup run
    no older than max_age_hours.

    Field names measured on a real backup run: status, endTime, type.
    """
    problems: list[str] = []
    if max_age_hours <= 0:
        return ["max_age_hours must be positive"]
    cutoff = now - timedelta(hours=max_age_hours)

    fresh: list[str] = []
    for index, item in enumerate(backups):
        if not isinstance(item, Mapping):
            problems.append(f"backup[{index}] is not an object")
            continue
        if item.get("status") != "SUCCESSFUL":
            continue
        end_time = item.get("endTime")
        if not isinstance(end_time, str) or not end_time:
            problems.append(
                f"backup {item.get('id')!r} is SUCCESSFUL but has no endTime"
            )
            continue
        try:
            finished = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
        except ValueError:
            problems.append(f"backup {item.get('id')!r} has an unreadable endTime")
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        if finished >= cutoff:
            fresh.append(str(item.get("id")))

    if not fresh:
        problems.append(
            f"no SUCCESSFUL backup finished within {max_age_hours}h; "
            "take one before the irreversible step"
        )
    return problems
