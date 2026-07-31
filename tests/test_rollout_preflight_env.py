"""The CLI must refuse an environment entry it cannot read, not skip it."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from databridge.rollout_checks import RolloutCheckError

_SPEC = importlib.util.spec_from_file_location(
    "rollout_preflight", Path(__file__).resolve().parents[1] / "scripts" / "rollout_preflight.py"
)
assert _SPEC is not None and _SPEC.loader is not None
preflight: Any = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)


def test_env_entries_reject_a_malformed_item() -> None:
    """Skipping it would let a check succeed beside something it never inspected."""
    good = [{"name": "A", "value": "1"}]
    assert preflight._env_entries(good, label="job x") == good
    for malformed in ([{"name": "A", "value": "1"}, "oops"], ["oops"], [None], [[]]):
        with pytest.raises(RolloutCheckError):
            preflight._env_entries(malformed, label="job x")
    with pytest.raises(RolloutCheckError):
        preflight._env_entries({"name": "A"}, label="service")
