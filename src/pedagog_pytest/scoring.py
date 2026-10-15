"""Rubric scoring: tests award points per rubric item instead of all-or-nothing.

A single-item test uses ``@rubric_item(...)`` and the ``score`` fixture; a
multi-item test declares a ``Rubric`` subclass, applies it with
``@rubric(...)``, and gets an instance of it as the ``scores`` fixture::

    class Design(Rubric):
        memory_safe: Score = item("Passes valgrind with no leaks", points=10)
        recursion: Score = item("Uses recursion", points=2, extra_credit=True)

    @rubric(Design)
    def test_design(scores: Design):
        with scores.memory_safe.check():
            assert leaks() == 0
        scores.recursion.award(points=1)

An item that is never awarded scores 0. Awards stand even if the test fails
afterwards; the test's status and its score are independent.
"""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Final, cast


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


@dataclass(frozen=True)
class ItemSpec:
    """A rubric item's definition (its id comes from the attribute name)."""

    name: str
    points: float
    extra_credit: bool = False

    def __post_init__(self) -> None:
        if self.points < 0:
            raise ValueError(f"rubric item points must be >= 0, got {self.points!r}")


class Score:
    """The points earned on one rubric item during one test."""

    def __init__(self, id: str, name: str, points: float, extra_credit: bool = False) -> None:
        self.id = id
        self.name = name
        self.points = float(points)
        self.extra_credit = extra_credit
        self.awarded = 0.0
        self.failures: list[str] = []

    def award(self, points: float | Full = FULL) -> None:
        """Set this item's points (replacing any earlier award); all of them by default."""
        amount = self.points if isinstance(points, Full) else float(points)
        if not 0 <= amount <= self.points:
            raise ValueError(f"rubric item {self.id!r}: {amount:g} is outside 0..{self.points:g}")
        self.awarded = amount

    def nil(self) -> None:
        """Back to 0 points, as if never awarded."""
        self.awarded = 0.0

    @contextmanager
    def check(self, points: float | Full = FULL) -> Iterator[None]:
        """Award ``points`` if the block completes; if it raises, the item gets 0,
        the failure is recorded on the item, and the test carries on (it still
        ends as failed). Time limits and other non-Exception errors propagate."""
        try:
            yield
        except Exception as e:
            self.nil()
            self.failures.append("".join(traceback.format_exception(type(e), e, e.__traceback__)).rstrip())
        else:
            self.award(points)


def item(name: str, points: float, *, extra_credit: bool = False) -> Score:
    """Declare a rubric item on a ``Rubric`` subclass; the attribute name is its id.

    Typed as ``Score`` because that's what the attribute is on the instance the
    test receives.
    """
    return cast(Score, ItemSpec(name=name, points=points, extra_credit=extra_credit))


class Rubric:
    """Base class for multi-item rubrics; subclasses declare items with ``item()``.

    Extra-credit items add to the score but not to the total.
    """

    _pedagog_specs: ClassVar[dict[str, ItemSpec]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        specs = dict(getattr(cls, "_pedagog_specs", {}))
        specs.update({k: v for k, v in vars(cls).items() if isinstance(v, ItemSpec)})
        if not specs:
            raise TypeError(f"rubric {cls.__name__} declares no items")
        cls._pedagog_specs = specs

    def __init__(self) -> None:
        for item_id, spec in self._pedagog_specs.items():
            setattr(self, item_id, Score(item_id, spec.name, spec.points, spec.extra_credit))

    def items(self) -> list[Score]:
        """This test's scores, in declaration order."""
        return [cast(Score, getattr(self, item_id)) for item_id in self._pedagog_specs]
