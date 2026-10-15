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

Points only come from explicit calls: an item that is never awarded scores 0.
Awards add up, stand even if the test fails afterwards, and the item's clamp is
applied once to the final total.
"""

from __future__ import annotations

import enum
import math
import os
import sysconfig
import traceback
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


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


class Score:
    """The points earned on one rubric item during one test."""

    def __init__(
        self,
        id: str,
        name: str,
        points: float = 0.0,
        clamp: Clamp = DEFAULT_CLAMP,
        extra_credit: ExtraCredit = ExtraCredit.NONE,
    ) -> None:
        _validate(points, clamp)
        self.id = id
        self.name = name
        self.points = float(points)
        self.clamp = clamp
        self.extra_credit = ExtraCredit(extra_credit)
        self.total = 0.0
        self.attempted = False
        self.lines: list[str] = []
        self.steps: list[str] = []
        self.failed_checks = 0
        # Set by the plugin: failure paths are shown relative to the test suite.
        self.root: Path | None = None

    @property
    def bounds(self) -> tuple[float | None, float | None]:
        if self.clamp is None:
            return (None, None)
        low, high = (self.points if isinstance(b, Full) else b for b in self.clamp)
        return (None if low is None else float(low), None if high is None else float(high))

    @property
    def final(self) -> float:
        """The running total, clamped."""
        low, high = self.bounds
        value = self.total
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
    def max(self) -> float:
        """What the item adds to the test's maximum."""
        return self.points if self.counts_in_max else 0.0

    def _amount(self, points: float | Full) -> float:
        amount = max(self.points - self.total, 0.0) if isinstance(points, Full) else float(points)
        if not math.isfinite(amount):
            raise ValueError(f"rubric item {self.id!r}: points must be finite, got {points!r}")
        return amount

    def _add(self, amount: float) -> None:
        self.total += amount
        if amount > 0:
            self.attempted = True

    def award(self, points: float | Full = FULL, reason: str | None = None) -> None:
        """Add points (negative to deduct); by default the rest of the item's points."""
        amount = self._amount(points)
        self._add(amount)
        label = f"FULL ({_signed(amount)})" if isinstance(points, Full) else _signed(amount)
        self.steps.append(f"award {label}{'  ' + reason if reason else ''}  = {_num(self.total)}")
        if reason:
            self.lines.append(f"{_signed(amount)} {reason}")

    def set(self, points: float, reason: str | None = None) -> None:
        """Replace the running total (for computed scores)."""
        self.total = self._amount(points)
        if self.total > 0:
            self.attempted = True
        self.steps.append(f"set {_num(self.total)}{'  ' + reason if reason else ''}  = {_num(self.total)}")
        if reason:
            self.lines.append(f"={_num(self.total)} {reason}")

    def nil(self) -> None:
        """Back to 0 points, with no feedback and not attempted, as if never touched."""
        self.total = 0.0
        self.attempted = False
        self.lines.clear()
        self.steps.append("nil()  = 0 (feedback and attempted cleared)")

    def attempt(self) -> None:
        """Mark the item attempted without awarding points (for ``ATTEMPTED_WORK``)."""
        self.attempted = True
        self.steps.append("attempt()  attempted")

    @contextmanager
    def check(self, points: float | Full = FULL, reason: str | None = None) -> Iterator[None]:
        """Add ``points`` if the block completes. If it raises, add nothing,
        record the failure on the item, and carry on (the test still ends as
        failed). Time limits and skips propagate."""
        try:
            yield
        except (Exception, pytest.fail.Exception) as e:
            worth = self._amount(points)
            failure = describe_failure(e, self.root)
            self.failed_checks += 1
            label = f"{reason} " if reason else ""
            self.steps.append(f"check ✗ +0  {label}(worth {_num(worth)}): {type(e).__name__}  = {_num(self.total)}")
            self.lines.append(f"✗ +0 {label}(worth {_num(worth)})\n{_indent(failure)}")
        else:
            amount = self._amount(points)
            self._add(amount)
            self.steps.append(f"check ✓ {_signed(amount)}{'  ' + reason if reason else ''}  = {_num(self.total)}")
            if reason:
                self.lines.append(f"✓ {_signed(amount)} {reason}")

    def feedback(self) -> str:
        """What the student is told about this item (subject to output visibility)."""
        return "\n".join(self.lines)

    def header(self) -> str:
        low, high = self.bounds
        clamp = f"[{_bound(low, '-inf')}, {_bound(high, 'inf')}]"
        return f"points {_num(self.points)}, clamp {clamp}, {self.extra_credit.value}"

    def trace(self) -> list[str]:
        """How the item's score was calculated, step by step (for instructors)."""
        lines = list(self.steps) if self.steps else ["(never awarded)"]
        final = f"final {_num(self.total)}"
        if self.final != self.total:
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

    def __post_init__(self) -> None:
        _validate(self.points, self.clamp)
        if self.id is not None and not (isinstance(self.id, str) and self.id.strip()):
            raise ValueError(f"rubric item id must be a non-empty string, got {self.id!r}")


def item(
    name: str | None = None,
    points: float = 0.0,
    *,
    id: str | None = None,
    clamp: Clamp = DEFAULT_CLAMP,
    extra_credit: ExtraCredit = ExtraCredit.NONE,
) -> Score:
    """Declare a rubric item on a ``Rubric`` subclass.

    ``id`` defaults to the attribute name and ``name`` to the id. ``clamp`` bounds
    the final total (``FULL`` is the item's points; ``None`` leaves a side, or
    with ``clamp=None`` both sides, unbounded). Typed as ``Score`` because that's
    what the attribute is on the instance the test receives.
    """
    return cast(Score, ItemSpec(id, name, float(points), clamp, ExtraCredit(extra_credit)))


class Rubric:
    """Base class for multi-item rubrics; subclasses declare items with ``item()``."""

    _pedagog_specs: ClassVar[dict[str, ItemSpec]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        specs = dict(getattr(cls, "_pedagog_specs", {}))
        specs.update({k: v for k, v in vars(cls).items() if isinstance(v, ItemSpec)})
        if not specs:
            raise TypeError(f"rubric {cls.__name__} declares no items")
        ids = [spec.id or attr for attr, spec in specs.items()]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise TypeError(f"rubric {cls.__name__} has duplicate item ids: {', '.join(duplicates)}")
        cls._pedagog_specs = specs

    def __init__(self) -> None:
        for attr, spec in self._pedagog_specs.items():
            item_id = spec.id or attr
            setattr(self, attr, Score(item_id, spec.name or item_id, spec.points, spec.clamp, spec.extra_credit))

    def items(self) -> list[Score]:
        """This test's scores, in declaration order."""
        return [cast(Score, getattr(self, attr)) for attr in self._pedagog_specs]
