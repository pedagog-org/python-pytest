"""Decorators that attach pedagog metadata to tests.

Each decorator attaches a pytest mark that the pedagog plugin reads when it
writes results.yaml. A test without a decorator gets the default shown in
that decorator's signature.
"""

from __future__ import annotations

import enum
from typing import Final, overload

import pytest

from .scoring import Rubric


class Visibility(str, enum.Enum):
    """When students can see one part of a test's result (its score or its output)."""

    VISIBLE = "visible"
    """Immediately, as soon as the run finishes."""
    HIDDEN = "hidden"
    """Never shown to students."""
    AFTER_END = "after_end"
    """Once the student can no longer submit: the late deadline if late
    submissions are allowed, otherwise the due date."""
    AFTER_PUBLISH = "after_publish"
    """When the instructor publishes results."""


class Expect(str, enum.Enum):
    """What the reference solution should do on a test during validation."""

    PASS = "pass"
    FAIL = "fail"


VISIBLE: Final = Visibility.VISIBLE
HIDDEN: Final = Visibility.HIDDEN
AFTER_END: Final = Visibility.AFTER_END
AFTER_PUBLISH: Final = Visibility.AFTER_PUBLISH

DEFAULT_WEIGHT: Final = 1.0
DEFAULT_SCORE_VISIBILITY: Final = Visibility.VISIBLE
DEFAULT_OUTPUT_VISIBILITY: Final = Visibility.AFTER_END
DEFAULT_TIMEOUT: Final = 10.0


def weight(points: float = DEFAULT_WEIGHT) -> pytest.MarkDecorator:
    """How many points the test is worth.

    A passing test scores all of them; any other outcome scores 0. The
    assignment's total is the sum of all weights.
    """
    if points < 0:
        raise ValueError(f"weight must be >= 0, got {points!r}")
    return pytest.mark.pedagog_weight(float(points))


def rubric_item(name: str | None = None, points: float = DEFAULT_WEIGHT) -> pytest.MarkDecorator:
    """Make the test a single rubric item scored through the ``score`` fixture.

    Points come only from ``score.award()`` (or a ``score.check()`` block); a
    test that never awards scores 0 even if it passes, and awards stand even if
    it fails afterwards. ``name`` defaults to the test's name (None). Replaces
    ``@weight``: ``points`` is the test's maximum.
    """
    if points < 0:
        raise ValueError(f"rubric item points must be >= 0, got {points!r}")
    return pytest.mark.pedagog_rubric_item(name=name, points=float(points))


def rubric(cls: type[Rubric]) -> pytest.MarkDecorator:
    """Score the test by a multi-item ``Rubric``; the ``scores`` fixture is an instance of it.

    The test's maximum is the sum of its non-extra-credit items; extra-credit
    items add to the score but not the maximum. Replaces ``@weight``.
    """
    if not (isinstance(cls, type) and issubclass(cls, Rubric) and cls is not Rubric):
        raise TypeError(f"@rubric needs a Rubric subclass, got {cls!r}")
    # with_args: called with a lone class, a mark decorates that class instead.
    return pytest.mark.pedagog_rubric.with_args(cls)


def number(value: str | int) -> pytest.MarkDecorator:
    """The test's label and position in the results, e.g. ``1`` or ``"2.3"``.

    Results are ordered by number, comparing dotted parts numerically
    (``1.2`` before ``1.10``); tests without a number follow the numbered
    ones in the order pytest collected them.
    """
    text = str(value).strip()
    if not text:
        raise ValueError("number must not be empty")
    return pytest.mark.pedagog_number(text)


@overload
def visibility(level: Visibility, /) -> pytest.MarkDecorator: ...


@overload
def visibility(
    *,
    score: Visibility = DEFAULT_SCORE_VISIBILITY,
    output: Visibility = DEFAULT_OUTPUT_VISIBILITY,
) -> pytest.MarkDecorator: ...


def visibility(
    level: Visibility | None = None,
    /,
    *,
    score: Visibility = DEFAULT_SCORE_VISIBILITY,
    output: Visibility = DEFAULT_OUTPUT_VISIBILITY,
) -> pytest.MarkDecorator:
    """When students see the test's score and its output (failure messages).

    ``@visibility(AFTER_END)`` sets both parts at once; ``@visibility(score=...,
    output=...)`` sets them separately. Output can reveal what a test checks, so
    by default it waits for the deadline while the score shows immediately.
    """
    if level is not None:
        if (score, output) != (DEFAULT_SCORE_VISIBILITY, DEFAULT_OUTPUT_VISIBILITY):
            raise TypeError("pass either one visibility for both parts, or score=/output=, not both")
        score = output = level
    return pytest.mark.pedagog_visibility(score=Visibility(score), output=Visibility(output))


def solution_expect(outcome: Expect) -> pytest.MarkDecorator:
    """What the reference solution should do on this test during validation.

    Undecorated tests are expected to pass. Mark a test ``Expect.FAIL`` when the
    reference solution deliberately doesn't satisfy it (e.g. a stretch goal);
    validation fails if the solution's outcome differs from the expectation.
    """
    return pytest.mark.pedagog_solution_expect(Expect(outcome))


def timeout(seconds: float = DEFAULT_TIMEOUT) -> pytest.MarkDecorator:
    """The test's time limit in seconds; 0 disables it.

    A test that runs longer fails. Under process isolation the student's
    process is also killed, so one hang can't affect later tests.
    """
    if seconds < 0:
        raise ValueError(f"timeout must be >= 0, got {seconds!r}")
    return pytest.mark.pedagog_timeout(float(seconds))
