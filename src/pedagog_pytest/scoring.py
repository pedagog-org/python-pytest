"""Rubric scoring: tests award points per rubric item instead of all-or-nothing.

A single-item test uses ``@rubric_item(...)`` and the ``score`` fixture; a
multi-item test declares a ``Rubric`` subclass, applies it with
``@rubric(...)``, and gets an instance of it as the ``scores`` fixture::

    class Design(Rubric):
        memory_safe: Score = item("Passes valgrind with no leaks", points=10)
        recursion: Score = item("Uses recursion", points=2, extra_credit=ExtraCredit.BONUS)

    @rubric(Design)
    def test_design(scores: Design):
        with scores.memory_safe.check(reason="no leaks"):
            assert leaks() == 0
        scores.recursion.award(1, reason="recursive helper")

Rubrics nest: ``part(...)`` places another ``Rubric`` inside one, to any depth
(up to ``MAX_DEPTH``). A part scores its own checks (with its own ``points``)
plus its children's, and ``worth=`` rescales a sub-rubric reused at another
weight.

Points only come from explicit calls: an item that is never awarded scores 0.
Awards add up, stand even if the test fails afterwards, and each item's (and
part's) clamp is applied once to its final total.
"""

from __future__ import annotations

import enum
import math
import os
import sysconfig
import traceback
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Final, Optional, Tuple, Union, cast

import pytest


class Full:
    """Sentinel for "all of the item's points"."""

    _instance: ClassVar[Full | None] = None

    def __new__(cls) -> Full:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "FULL"


FULL: Final = Full()

# Runtime aliases, so they can't use PEP 604 syntax on Python 3.10 with None.
Bound = Union[float, Full, None]
Clamp = Optional[Tuple[Bound, Bound]]
DEFAULT_CLAMP: Final[Clamp] = (0.0, FULL)
# How deep parts may nest below a test (pedagog refuses deeper results).
MAX_DEPTH: Final = 8
# When students see a part's score or feedback (see markers.Visibility).
VISIBILITIES: Final = ("visible", "hidden", "after_end", "after_publish")


class ExtraCredit(str, enum.Enum):
    """How an item counts toward the maximum score."""

    NONE = "none"
    """A regular item: counts in the score and the max."""
    BONUS = "bonus"
    """Counts in the score, never in the max."""
    ATTEMPTED_WORK = "attempted-work"
    """Counts in the score, and in the max only once attempted (any points
    awarded, or ``attempt()`` called)."""


def _num(x: float) -> str:
    return f"{x:g}"


def _signed(x: float) -> str:
    return f"{x:+g}"


def _bound(x: float | None, infinity: str) -> str:
    return infinity if x is None else _num(x)


def _validate(points: float, clamp: Clamp) -> None:
    if not math.isfinite(points) or points < 0:
        raise ValueError(f"rubric item points must be >= 0, got {points!r}")
    if clamp is None:
        return
    if not (isinstance(clamp, tuple) and len(clamp) == 2):
        raise TypeError(f"clamp must be (low, high) or None, got {clamp!r}")
    low, high = (points if isinstance(b, Full) else b for b in clamp)
    for b in (low, high):
        if b is not None and not (isinstance(b, (int, float)) and math.isfinite(b)):
            raise ValueError(f"clamp bounds must be numbers, FULL or None, got {clamp!r}")
    if low is not None and high is not None and low > high:
        raise ValueError(f"clamp low {low:g} exceeds high {high:g}")


_PACKAGE_DIR: Final = os.path.dirname(os.path.abspath(__file__))
_STDLIB_DIR: Final = os.path.abspath(sysconfig.get_paths()["stdlib"])


def _suite_frame(filename: str) -> bool:
    path = os.path.abspath(filename)
    return not (
        path.startswith(_PACKAGE_DIR)
        or path.startswith(_STDLIB_DIR)
        or "site-packages" in path
        or "dist-packages" in path
        or filename.startswith("<")
    )


def describe_failure(error: BaseException, root: Path | None = None) -> str:
    """One readable account of a failure: where in the test suite it happened,
    and the error, without helper, pytest or RPyC frames."""
    frames = [f for f in traceback.extract_tb(error.__traceback__) if _suite_frame(f.filename)]
    where = ""
    if frames:
        filename = frames[-1].filename
        if root is not None:
            try:
                filename = os.path.relpath(filename, root)
            except ValueError:
                pass
        where = f"{filename}:{frames[-1].lineno}: "
    message = str(error).strip()
    text = where + type(error).__name__ + (f": {message}" if message else "")
    for note in getattr(error, "__notes__", None) or ():
        text += "\n" + str(note)
    return text


def assert_message(error: BaseException) -> str | None:
    """The message a test wrote on a failing ``assert`` (``assert x, "msg"``),
    without pytest's explanation of the values; ``None`` if it wrote none or
    the error isn't an assertion. Messages are for students; values may not be."""
    if not isinstance(error, AssertionError) or getattr(error, "_pedagog_from_student", False):
        return None
    # Only an assert in the test itself: one raised in student code (or deeper)
    # wrote its own message, not the instructor's.
    # (check() sees the exception through its own frame, so compare the suite's
    # frames: the test's file first, the file it was raised in last.)
    frames = [f for f in traceback.extract_tb(error.__traceback__) if _suite_frame(f.filename)]
    if frames and frames[-1].filename != frames[0].filename:
        return None
    text = str(error)
    # pytest's rewritten asserts: "<message>\nassert <expr>..." or just "assert <expr>...".
    if text.startswith("assert ") or not text.strip():
        return None
    message = text.split("\nassert ", 1)[0].strip()
    return message or None


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _visibility(part: str, value: object) -> str | None:
    if value is None:
        return None
    text = str(getattr(value, "value", value))
    if text not in VISIBILITIES:
        raise ValueError(f"{part} visibility must be one of {', '.join(VISIBILITIES)}, got {value!r}")
    return text


class Score:
    """The points earned on one rubric item (or part) during one test.

    Points are recorded scaled: ``scale`` is the factor a ``worth=`` above
    applies (1 without one), so a test scores in its rubric's own terms."""

    def __init__(
        self,
        id: str,
        name: str,
        points: float = 0.0,
        clamp: Clamp = DEFAULT_CLAMP,
        extra_credit: ExtraCredit = ExtraCredit.NONE,
        *,
        scale: float = 1.0,
        visibility: dict[str, str] | None = None,
    ) -> None:
        _validate(points, clamp)
        self.id = id
        self.name = name
        self.scale = scale
        self.points = float(points) * scale
        self.clamp = clamp
        self.extra_credit = ExtraCredit(extra_credit)
        # A part's own score/feedback visibility (tighter than the test's).
        self.visibility = dict(visibility or {})
        # A part's items and parts (empty for an item).
        self.children: list[Score] = []
        # The factor this part's own worth= applied (for the trace and results).
        self.local_scale: float | None = None
        self.total = 0.0
        self._attempted = False
        # What students are told (feedback): checks, and awards with a reason.
        self.feedback_steps: list[dict[str, object]] = []
        # How the score came about, for instructors (the trace).
        self.steps: list[str] = []
        # What failed checks raised (the test's error).
        self.failures: list[str] = []
        # The failures for students (see sanitize.py).
        self.sanitized: list[str] = []
        self.failed_checks = 0
        # Set by the plugin: failure paths are shown relative to the test suite.
        self.root: Path | None = None

    @property
    def attempted(self) -> bool:
        """Any points awarded (or ``attempt()``), here or in a part below."""
        return self._attempted or any(c.attempted for c in self.children)

    @attempted.setter
    def attempted(self, value: bool) -> None:
        self._attempted = value

    @property
    def full(self) -> float:
        """Everything it could earn: its own points and its children's (extra credit included)."""
        return self.points + sum(c.full for c in self.children)

    @property
    def bounds(self) -> tuple[float | None, float | None]:
        if self.clamp is None:
            return (None, None)
        low, high = (self.full if isinstance(b, Full) else None if b is None else b * self.scale for b in self.clamp)
        return (None if low is None else float(low), None if high is None else float(high))

    @property
    def subtotal(self) -> float:
        """Its own checks and awards, plus its children's (each clamped)."""
        return self.total + sum(c.final for c in self.children)

    @property
    def final(self) -> float:
        """The running total, clamped."""
        low, high = self.bounds
        value = self.subtotal
        if low is not None:
            value = max(value, low)
        if high is not None:
            value = min(value, high)
        return value

    @property
    def counts_in_max(self) -> bool:
        if self.extra_credit is ExtraCredit.NONE:
            return True
        return self.extra_credit is ExtraCredit.ATTEMPTED_WORK and self.attempted

    @property
    def declared_max(self) -> float:
        """Its own points and its children's counted maximum."""
        return self.points + sum(c.max for c in self.children)

    @property
    def max(self) -> float:
        """What it adds to its parent's (or the test's) maximum."""
        return self.declared_max if self.counts_in_max else 0.0

    def walk(self) -> Iterator[Score]:
        """This node and every part and item below it."""
        yield self
        for c in self.children:
            yield from c.walk()

    def _amount(self, points: float | Full) -> float:
        amount = max(self.points - self.total, 0.0) if isinstance(points, Full) else float(points) * self.scale
        if not math.isfinite(amount):
            raise ValueError(f"rubric item {self.id!r}: points must be finite, got {points!r}")
        return amount

    def _add(self, amount: float) -> None:
        self.total += amount
        if amount > 0:
            self._attempted = True

    def _step(self, id: str | None, step: dict[str, object]) -> None:
        if id is not None:
            if not (isinstance(id, str) and id.strip()):
                raise ValueError(f"step id must be a non-empty string, got {id!r}")
            if any(s.get("id") == id for s in self.feedback_steps):
                raise ValueError(f"step id {id!r} is already used in item {self.id!r}")
            step = {"id": id, **step}
        self.feedback_steps.append(step)

    def award(self, points: float | Full = FULL, reason: str | None = None, *, id: str | None = None) -> None:
        """Add points (negative to deduct); by default the rest of the item's points.

        With a ``reason``, the award is a step of the item's feedback; ``id``
        names that step (unique within the item)."""
        amount = self._amount(points)
        self._add(amount)
        label = f"FULL ({_signed(amount)})" if isinstance(points, Full) else _signed(amount)
        self.steps.append(f"award {label}{'  ' + reason if reason else ''}  = {_num(self.total)}")
        if reason or id is not None:
            self._step(id, {"points": amount, **({"reason": reason} if reason else {})})

    def set(self, points: float, reason: str | None = None) -> None:
        """Replace the running total (for computed scores). Shown in the trace,
        not in the feedback."""
        self.total = self._amount(points)
        if self.total > 0:
            self._attempted = True
        self.steps.append(f"set {_num(self.total)}{'  ' + reason if reason else ''}  = {_num(self.total)}")

    def nil(self) -> None:
        """Back to 0 points, with no feedback and not attempted, as if never touched."""
        self.total = 0.0
        self._attempted = False
        self.feedback_steps.clear()
        self.steps.append("nil()  = 0 (feedback and attempted cleared)")

    def attempt(self) -> None:
        """Mark the item attempted without awarding points (for ``ATTEMPTED_WORK``)."""
        self._attempted = True
        self.steps.append("attempt()  attempted")

    @contextmanager
    def check(
        self, points: float | Full = FULL, reason: str | None = None, *, id: str | None = None
    ) -> Iterator[None]:
        """Add ``points`` if the block completes. If it raises, add nothing,
        record the failure, and carry on (the test still ends as failed). Time
        limits and skips propagate.

        Every check is a step of the item's feedback; a failed one carries the
        assert's message, if the test wrote one. What it raised goes to the
        test's error. ``id`` names the step (unique within the item)."""
        label = f"{reason} " if reason else ""
        try:
            yield
        except (Exception, pytest.fail.Exception) as e:
            worth = self._amount(points)
            failure = describe_failure(e, self.root)
            self.failed_checks += 1
            self.failures.append(failure)
            from .sanitize import sanitize  # sanitize imports this module

            for_students = sanitize(e, self.root)
            if for_students is not None:
                self.sanitized.append(for_students)
            self.steps.append(f"check ✗ +0  {label}(worth {_num(worth)}): {type(e).__name__}  = {_num(self.total)}")
            step: dict[str, object] = {"result": "fail", "points": 0.0, "worth": worth}
            if reason:
                step["reason"] = reason
            message = assert_message(e)
            if message:
                step["message"] = message
            self._step(id, step)
        else:
            amount = self._amount(points)
            self._add(amount)
            self.steps.append(f"check ✓ {_signed(amount)}{'  ' + reason if reason else ''}  = {_num(self.total)}")
            self._step(
                id, {"result": "pass", "points": amount, "worth": amount, **({"reason": reason} if reason else {})}
            )

    def header(self) -> str:
        low, high = self.bounds
        clamp = f"[{_bound(low, '-inf')}, {_bound(high, 'inf')}]"
        head = f"points {_num(self.points)}, clamp {clamp}, {self.extra_credit.value}"
        if self.local_scale is not None:
            head += f", parts scaled ×{self.local_scale:.4g}"
        return head

    def trace(self) -> list[str]:
        """How the item's score was calculated, step by step (for instructors)."""
        lines = list(self.steps) if self.steps else (["(never awarded)"] if not self.children else [])
        for c in self.children:
            lines.append(f"{c.id}  {c.header()}")
            lines += _indent("\n".join(c.trace())).splitlines()
        final = f"final {_num(self.subtotal)}"
        if self.children:
            final = f"final {_num(self.total)} own + {_num(self.subtotal - self.total)} parts = {_num(self.subtotal)}"
        if self.final != self.subtotal:
            final += f" -> clamped to {_num(self.final)}"
        if self.extra_credit is ExtraCredit.BONUS:
            final += "; bonus: not in max"
        elif self.extra_credit is ExtraCredit.ATTEMPTED_WORK:
            final += "; attempted: counts in max" if self.attempted else "; not attempted: not in max"
        lines.append(f"{final}  -> {_num(self.final)} / {_num(self.max)}")
        return lines


@dataclass(frozen=True)
class ItemSpec:
    """A rubric item's definition; its id defaults to the attribute name."""

    id: str | None
    name: str | None
    points: float
    clamp: Clamp
    extra_credit: ExtraCredit
    score: str | None = None
    feedback: str | None = None

    def __post_init__(self) -> None:
        _validate(self.points, self.clamp)
        if self.id is not None and not (isinstance(self.id, str) and self.id.strip()):
            raise ValueError(f"rubric item id must be a non-empty string, got {self.id!r}")

    @property
    def visibility(self) -> dict[str, str]:
        return {k: v for k, v in (("score", self.score), ("feedback", self.feedback)) if v is not None}


def item(
    name: str | None = None,
    points: float = 0.0,
    *,
    id: str | None = None,
    clamp: Clamp = DEFAULT_CLAMP,
    extra_credit: ExtraCredit = ExtraCredit.NONE,
    score: object = None,
    feedback: object = None,
) -> Score:
    """Declare a rubric item on a ``Rubric`` subclass.

    ``id`` defaults to the attribute name and ``name`` to the id. ``clamp`` bounds
    the final total (``FULL`` is the item's points; ``None`` leaves a side, or
    with ``clamp=None`` both sides, unbounded). ``score`` and ``feedback`` hide
    this item's parts of the result longer than the test's visibility says
    (they can only tighten it). Typed as ``Score`` because that's what the
    attribute is on the instance the test receives.
    """
    return cast(
        Score,
        ItemSpec(
            id, name, float(points), clamp, ExtraCredit(extra_credit),
            _visibility("score", score), _visibility("feedback", feedback),
        ),
    )


@dataclass(frozen=True)
class PartSpec:
    """A nested rubric's place in another: its own points, and its weight."""

    id: str | None
    name: str | None
    points: float
    worth: float | None
    clamp: Clamp
    extra_credit: ExtraCredit
    score: str | None
    feedback: str | None
    rubric: type[Rubric] | None

    def __post_init__(self) -> None:
        _validate(self.points, self.clamp)
        if self.id is not None and not (isinstance(self.id, str) and self.id.strip()):
            raise ValueError(f"part id must be a non-empty string, got {self.id!r}")
        if self.worth is not None and not (math.isfinite(self.worth) and self.worth >= self.points):
            raise ValueError(f"part worth must be a number >= its own points ({self.points:g}), got {self.worth!r}")

    @property
    def visibility(self) -> dict[str, str]:
        return {k: v for k, v in (("score", self.score), ("feedback", self.feedback)) if v is not None}


def part(
    name: str | None = None,
    *,
    points: float = 0.0,
    worth: float | None = None,
    id: str | None = None,
    clamp: Clamp = DEFAULT_CLAMP,
    extra_credit: ExtraCredit = ExtraCredit.NONE,
    score: object = None,
    feedback: object = None,
    rubric: type[Rubric] | None = None,
) -> Any:
    """Place another ``Rubric`` inside this one, as a part.

    The sub-rubric is the attribute's annotation (``square: Square = part(...)``),
    or ``rubric=``. ``points`` are the part's own, for checks placed directly on
    it; its score is those plus its children's, clamped. ``worth`` is the part's
    total (its own points included): the sub-rubric is rescaled to fit, and tests
    keep scoring in its own terms. Extra credit inside scales by the same factor
    without being part of the target. On an extra-credit part, ``worth`` is its
    bonus value. ``score`` and ``feedback`` can only tighten the test's
    visibility.
    """
    return PartSpec(
        id, name, float(points), None if worth is None else float(worth), clamp, ExtraCredit(extra_credit),
        _visibility("score", score), _visibility("feedback", feedback), rubric,
    )


class Rubric(Score):
    """Base class for rubrics; subclasses declare items with ``item()`` and
    nested rubrics with ``part()``. An instance is a part: it scores its own
    checks (its own ``points``) plus its items' and parts'."""

    _pedagog_specs: ClassVar[dict[str, ItemSpec | PartSpec]] = {}
    _pedagog_parts: ClassVar[dict[str, type[Rubric]] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        specs = dict(getattr(cls, "_pedagog_specs", {}))
        specs.update({k: v for k, v in vars(cls).items() if isinstance(v, (ItemSpec, PartSpec))})
        if not specs:
            raise TypeError(f"rubric {cls.__name__} declares no items")
        ids = [spec.id or attr for attr, spec in specs.items()]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise TypeError(f"rubric {cls.__name__} has duplicate item ids: {', '.join(duplicates)}")
        cls._pedagog_specs = specs
        cls._pedagog_parts = None

    @classmethod
    def _parts(cls) -> dict[str, type[Rubric]]:
        """Each part's sub-rubric, from ``rubric=`` or the annotation (resolved
        once, when first needed, so sub-rubrics may be declared in any order)."""
        if cls._pedagog_parts is None:
            hints: dict[str, Any] = {}
            try:
                hints = typing.get_type_hints(cls)
            except Exception:
                hints = {}
            found: dict[str, type[Rubric]] = {}
            for attr, spec in cls._pedagog_specs.items():
                if not isinstance(spec, PartSpec):
                    continue
                sub = spec.rubric or hints.get(attr)
                if not (isinstance(sub, type) and issubclass(sub, Rubric)):
                    raise TypeError(
                        f"part {cls.__name__}.{attr} needs its rubric: annotate it "
                        f"({attr}: SomeRubric = part(...)) or pass rubric=SomeRubric"
                    )
                found[attr] = sub
            cls._pedagog_parts = found
        return cls._pedagog_parts

    @classmethod
    def declared(cls, counted_only: bool = True) -> float:
        """The rubric's total at its declared points (only what counts in the max
        by default), parts at their worth."""
        total = 0.0
        parts = cls._parts()
        for attr, spec in cls._pedagog_specs.items():
            if counted_only and spec.extra_credit is not ExtraCredit.NONE:
                continue
            if isinstance(spec, ItemSpec):
                total += spec.points
            elif spec.worth is not None:
                total += spec.worth
            else:
                total += spec.points + parts[attr].declared(counted_only)
        return total

    def __init__(
        self,
        id: str = "",
        name: str = "",
        points: float = 0.0,
        clamp: Clamp = DEFAULT_CLAMP,
        extra_credit: ExtraCredit = ExtraCredit.NONE,
        *,
        scale: float = 1.0,
        visibility: dict[str, str] | None = None,
        depth: int = 0,
    ) -> None:
        super().__init__(
            id or type(self).__name__, name or id or type(self).__name__, points, clamp, extra_credit,
            scale=scale, visibility=visibility,
        )
        if depth > MAX_DEPTH:
            raise TypeError(f"rubric {type(self).__name__} nests deeper than {MAX_DEPTH} levels")
        # A test's own rubric (not a part) has no points of its own.
        self.top = depth == 0
        parts = self._parts()
        for attr, spec in self._pedagog_specs.items():
            node_id = spec.id or attr
            node: Score
            if isinstance(spec, ItemSpec):
                node = Score(
                    node_id, spec.name or node_id, spec.points, spec.clamp, spec.extra_credit,
                    scale=scale, visibility=spec.visibility,
                )
            else:
                sub = parts[attr]
                factor = 1.0
                if spec.worth is not None:
                    base = sub.declared()
                    if base <= 0 and spec.extra_credit is not ExtraCredit.NONE:
                        base = sub.declared(counted_only=False)
                    if base <= 0:
                        raise TypeError(
                            f"part {type(self).__name__}.{attr} (worth={spec.worth:g}): {sub.__name__} has no "
                            "counted points to scale; mark the part extra credit and put worth= on it"
                        )
                    factor = (spec.worth - spec.points) / base
                node = sub(
                    node_id, spec.name or node_id, spec.points, spec.clamp, spec.extra_credit,
                    scale=scale, visibility=spec.visibility, depth=depth + 1,
                )
                if spec.worth is not None:
                    node.local_scale = factor
                    node._rescale(factor)
            setattr(self, attr, node)
            self.children.append(node)

    def _rescale(self, factor: float) -> None:
        """Apply a worth= factor to everything below this part (not its own points)."""
        for c in self.children:
            for n in c.walk():
                n.scale *= factor
                n.points *= factor

    def _no_own_points(self) -> None:
        if self.top:
            raise TypeError(
                f"rubric {type(self).__name__} is the test's: it has no points of its own; "
                "score one of its items or parts"
            )

    def award(self, points: float | Full = FULL, reason: str | None = None, *, id: str | None = None) -> None:
        self._no_own_points()
        super().award(points, reason, id=id)

    @contextmanager
    def check(
        self, points: float | Full = FULL, reason: str | None = None, *, id: str | None = None
    ) -> Iterator[None]:
        self._no_own_points()
        with super().check(points, reason, id=id):
            yield

    def items(self) -> list[Score]:
        """This test's scores, in declaration order."""
        return list(self.children)
