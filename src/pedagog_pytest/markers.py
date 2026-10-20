"""Decorators that attach pedagog metadata to tests.

Each decorator attaches a pytest mark that the pedagog plugin reads when it
writes results.yaml. A test without a decorator gets the default shown in
that decorator's signature.
"""

from __future__ import annotations

import enum
from typing import Final, overload

import pytest

from .scoring import DEFAULT_CLAMP, Clamp, ExtraCredit, ItemSpec, Rubric


class Visibility(str, enum.Enum):
    """When students can see one part of a test's result: its score, feedback,
    stdout or error."""

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

DEFAULT_VISIBILITY: Final = Visibility.VISIBLE
# The error can reveal what a test expects (pytest shows the values and the
# test's source), so it waits for the deadline.
DEFAULT_ERROR_VISIBILITY: Final = Visibility.AFTER_END
PARTS: Final = ("score", "feedback", "stdout", "error")
DEFAULTS: Final = {
    "score": DEFAULT_VISIBILITY,
    "feedback": DEFAULT_VISIBILITY,
    "stdout": DEFAULT_VISIBILITY,
    "error": DEFAULT_ERROR_VISIBILITY,
}
DEFAULT_TIMEOUT: Final = 10.0


def _check_id(id: str | None) -> None:
    if id is not None and not (isinstance(id, str) and id.strip()):
        raise ValueError(f"id must be a non-empty string, got {id!r}")


def rubric_item(
    name: str | None = None,
    points: float = 0.0,
    *,
    id: str | None = None,
    clamp: Clamp = DEFAULT_CLAMP,
    extra_credit: ExtraCredit = ExtraCredit.NONE,
) -> pytest.MarkDecorator:
    """Score the test as one rubric item through the ``score`` fixture.

    Points come only from ``score.award()``, ``score.set()`` and
    ``score.check()``: a test that never awards scores 0 even if it passes, and
    awards stand even if it fails afterwards. ``id`` is the test's stable id
    (none by default); ``name`` overrides its display name. ``clamp`` bounds the
    final score (``FULL`` is ``points``; ``None`` leaves a side unbounded).
    """
    _check_id(id)
    # Validates points and clamp the same way rubric items are.
    ItemSpec(id, name, float(points), clamp, ExtraCredit(extra_credit))
    return pytest.mark.pedagog_rubric_item(
        id=id, name=name, points=float(points), clamp=clamp, extra_credit=ExtraCredit(extra_credit)
    )


def rubric(cls: type[Rubric], *, id: str | None = None, name: str | None = None) -> pytest.MarkDecorator:
    """Score the test by a multi-item ``Rubric``; the ``scores`` fixture is an instance of it.

    The test's score is the sum of its items' scores, and its maximum the sum of
    the items that count in it (see ``ExtraCredit``). ``id`` is the test's
    stable id (none by default); ``name`` overrides its display name.
    """
    if not (isinstance(cls, type) and issubclass(cls, Rubric) and cls is not Rubric):
        raise TypeError(f"@rubric needs a Rubric subclass, got {cls!r}")
    _check_id(id)
    # with_args: called with a lone class, a mark decorates that class instead.
    return pytest.mark.pedagog_rubric.with_args(cls, id=id, name=name)


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
    score: Visibility = DEFAULT_VISIBILITY,
    feedback: Visibility = DEFAULT_VISIBILITY,
    stdout: Visibility = DEFAULT_VISIBILITY,
    error: Visibility = DEFAULT_ERROR_VISIBILITY,
) -> pytest.MarkDecorator: ...


def visibility(
    level: Visibility | None = None,
    /,
    *,
    score: Visibility = DEFAULT_VISIBILITY,
    feedback: Visibility = DEFAULT_VISIBILITY,
    stdout: Visibility = DEFAULT_VISIBILITY,
    error: Visibility = DEFAULT_ERROR_VISIBILITY,
) -> pytest.MarkDecorator:
    """When students see each part of the test's result: its ``score`` (points
    and pass/fail), ``feedback`` (its checks and awards), ``stdout`` (what it
    printed) and ``error`` (what went wrong: failures and exceptions, with
    pytest's values, which can reveal what a test expects).

    ``@visibility(AFTER_END)`` sets every part at once; the keywords set them
    separately. Everything is visible by default but the error, which waits
    for the deadline; courses can override it.
    """
    parts = {"score": score, "feedback": feedback, "stdout": stdout, "error": error}
    if level is not None:
        if parts != DEFAULTS:
            raise TypeError("pass either one visibility for every part, or score=/feedback=/stdout=/error=, not both")
        parts = dict.fromkeys(parts, level)
    return pytest.mark.pedagog_visibility(**{k: Visibility(v) for k, v in parts.items()})


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
