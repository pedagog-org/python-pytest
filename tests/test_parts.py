"""Nested rubrics: parts with their own points, worth= rescaling, per-part
visibility, extra credit at any level, and results format 3."""

import json

import pytest

from pedagog_pytest import ExtraCredit, Rubric, Score, item, part


def run(pytester: pytest.Pytester, source: str) -> dict:
    pytester.makepyfile(test_suite=source)
    out = pytester.path / "out" / "results.yaml"
    pytester.runpytest("--pedagog-results", str(out))
    return json.loads(out.read_text())


def node(items: list, *path: str) -> dict:
    found: dict = {"parts": items}
    for p in path:
        found = next(n for n in found["parts"] if n["id"] == p)
    return found


SUITE = """
from pedagog_pytest import AFTER_END, ExtraCredit, Rubric, Score, item, part, rubric


class Spans(Rubric):
    count: Score = item("has rank(A) vectors", points=1)
    each: Score = item("each vector belongs", points=2)


class ColumnSpace(Rubric):
    pivots: Score = item("pivcols", points=1)
    spans: Spans = part("C spans col(A)")


class Bases(Rubric):
    col: ColumnSpace = part("Column space", feedback=AFTER_END)
    bonus: Score = item("rank theorem", points=1, extra_credit=ExtraCredit.BONUS)


class PartB(Rubric):
    a2: Bases = part("A2", worth=2)
    a4: Bases = part("A4 (given)", points=0.5)


def grade(b, columns_ok):
    with b.col.pivots.check(reason="pivcols"):
        pass
    with b.col.spans.count.check(reason="count"):
        pass
    for j, ok in enumerate(columns_ok, start=1):
        with b.col.spans.each.check(points=2 / len(columns_ok), reason=f"column {j}"):
            assert ok, f"column {j} isn't a pivot column"


@rubric(PartB, id="3b", name="Part B")
def test_part_b(scores: PartB):
    grade(scores.a2, [True, True])
    scores.a2.bonus.award(reason="rank + nullity = n")
    with scores.a4.check(points=0.5, reason="A4 is the given matrix"):
        pass
    grade(scores.a4, [True, False, True])
"""


def test_parts_nest_with_own_points_and_worth(pytester):
    results = run(pytester, SUITE)
    assert results["format"] == 3
    t = results["tests"][0]
    # A2: Bases (4 counted) scaled to 2, its bonus (1) scaled the same; A4: 0.5 own + 4.
    a2 = node(t["items"], "a2")
    assert (a2["max"], a2["scale"]) == (pytest.approx(2.0), pytest.approx(0.5))
    assert a2["score"] == pytest.approx(2.5), "2 counted + 0.5 bonus"
    assert node(t["items"], "a2", "bonus")["score"] == pytest.approx(0.5)
    assert node(t["items"], "a2", "col", "spans", "each")["max"] == pytest.approx(1.0)

    a4 = node(t["items"], "a4")
    assert (a4["points"], a4["max"]) == (0.5, 4.5)
    assert a4["steps"] == [{"result": "pass", "points": 0.5, "worth": 0.5, "reason": "A4 is the given matrix"}]
    each = node(t["items"], "a4", "col", "spans", "each")
    assert each["score"] == pytest.approx(4 / 3)
    assert each["steps"][1] == {
        "result": "fail", "points": 0.0, "worth": pytest.approx(2 / 3),
        "reason": "column 2", "message": "column 2 isn't a pivot column",
    }
    assert a4["score"] == pytest.approx(0.5 + 1 + 1 + 4 / 3)
    assert t["max"] == pytest.approx(6.5) and t["score"] == pytest.approx(2.5 + a4["score"])
    assert node(t["items"], "a4", "col")["visibility"] == {"feedback": "after_end"}
    assert t["status"] == "fail"
    assert "rubric checks failed: a4/col/spans/each" in t["error"]
    assert "parts scaled ×0.5" in t["trace"]
    assert results["summary"]["total"] == pytest.approx(6.5)


def test_a_crash_keeps_every_maximum(pytester):
    results = run(pytester, SUITE.replace('    grade(scores.a2, [True, True])\n', '    raise RuntimeError("boom")\n'))
    t = results["tests"][0]
    assert (t["score"], t["max"]) == (0.0, pytest.approx(6.5))
    assert node(t["items"], "a4", "col", "spans")["max"] == 3.0


def test_worth_needs_counted_points_unless_the_part_is_extra_credit():
    class Stretch(Rubric):
        a: Score = item("a", points=1, extra_credit=ExtraCredit.BONUS)
        b: Score = item("b", points=2, extra_credit=ExtraCredit.BONUS)

    class Regular(Rubric):
        stretch: Stretch = part("Stretch", worth=1)

    with pytest.raises(TypeError, match="mark the part extra credit and put worth= on it"):
        Regular()

    class Bonus(Rubric):
        stretch: Stretch = part("Stretch", worth=1, extra_credit=ExtraCredit.BONUS)

    r = Bonus()
    assert r.stretch.full == pytest.approx(1.0), "its bonus total scaled to its worth"
    assert r.max == 0.0, "a bonus part adds nothing to the maximum"


def test_part_declarations_are_checked():
    class Leaf(Rubric):
        x: Score = item("x", points=1)

    with pytest.raises(ValueError, match="worth must be a number >= its own points"):
        part("p", points=2, worth=1)
    with pytest.raises(ValueError, match="feedback visibility must be one of"):
        part("p", feedback="later")

    class NoRubric(Rubric):
        p = part("p")
        x: Score = item("x", points=1)

    with pytest.raises(TypeError, match="needs its rubric"):
        NoRubric()

    class Explicit(Rubric):
        p = part("p", rubric=Leaf)

    assert Explicit().max == 1.0


def test_parts_nest_at_most_eight_deep():
    class L9(Rubric):
        x: Score = item("x", points=1)

    deepest = L9
    for _ in range(8):
        deepest = type("L", (Rubric,), {"p": part("p", rubric=deepest)})
    deepest()  # 8 parts below the test
    too_deep = type("L", (Rubric,), {"p": part("p", rubric=deepest)})
    with pytest.raises(TypeError, match="deeper than 8"):
        too_deep()


def test_the_tests_own_rubric_has_no_points_of_its_own():
    class Top(Rubric):
        x: Score = item("x", points=1)

    with pytest.raises(TypeError, match="no points of its own"):
        Top().award(1)
