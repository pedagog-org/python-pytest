"""Test metadata decorators. Each attaches a pytest mark that the plugin reads."""

from __future__ import annotations

import pytest

VISIBILITIES = ("visible", "hidden", "after_end", "after_publish")
SOLUTION_OUTCOMES = ("pass", "fail")


def weight(points: float) -> pytest.MarkDecorator:
    """Points awarded when the test passes (default 1)."""
    if points < 0:
        raise ValueError(f"weight must be >= 0, got {points!r}")
    return pytest.mark.pedagog_weight(float(points))


def number(value: str | int) -> pytest.MarkDecorator:
    """Display number, e.g. ``1`` or ``"2.3"``."""
    return pytest.mark.pedagog_number(str(value))


def visibility(score: str | None = None, output: str | None = None) -> pytest.MarkDecorator:
    """When students see the test's score and its output.

    Each is one of ``visible``, ``hidden``, ``after_end``, ``after_publish``;
    unset parts keep the defaults (score visible, output after_end).
    """
    for part, value in (("score", score), ("output", output)):
        if value is not None and value not in VISIBILITIES:
            raise ValueError(f"visibility {part}={value!r}: expected one of {VISIBILITIES}")
    return pytest.mark.pedagog_visibility(score=score, output=output)


def solution_expect(outcome: str) -> pytest.MarkDecorator:
    """What the reference solution should do on this test (default ``pass``).

    Validation fails when the solution's outcome differs, so a test that is
    meant to fail against the reference solution must say so.
    """
    if outcome not in SOLUTION_OUTCOMES:
        raise ValueError(f"solution_expect({outcome!r}): expected one of {SOLUTION_OUTCOMES}")
    return pytest.mark.pedagog_solution_expect(outcome)


def timeout(seconds: float) -> pytest.MarkDecorator:
    """Time limit for this test in seconds (default 10; 0 disables)."""
    if seconds < 0:
        raise ValueError(f"timeout must be >= 0, got {seconds!r}")
    return pytest.mark.pedagog_timeout(float(seconds))
