"""Bind a golden set to the runtime search namespace before evaluation starts."""

from __future__ import annotations

import os
from collections.abc import Callable


class GoldenSpaceError(ValueError):
    """The requested or initialized runtime space disagrees with the golden set."""


def configure_golden_space(
    *, golden_space: str, cli_space: str | None, get_actual_space: Callable[[], str]
) -> str:
    """Set an absent env value and assert every space source agrees."""
    if cli_space is not None and cli_space != golden_space:
        raise GoldenSpaceError(
            f"--space {cli_space!r} does not match golden space_key {golden_space!r}"
        )
    env_space = os.environ.get("DATABRIDGE_SPACE")
    if env_space is not None and env_space != golden_space:
        raise GoldenSpaceError(
            "DATABRIDGE_SPACE "
            f"{env_space!r} does not match golden space_key {golden_space!r}"
        )
    if env_space is None:
        os.environ["DATABRIDGE_SPACE"] = golden_space
    actual_space = get_actual_space()
    if actual_space != golden_space:
        raise GoldenSpaceError(
            f"runtime space {actual_space!r} does not match golden space_key {golden_space!r}"
        )
    return actual_space
