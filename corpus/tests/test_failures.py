"""One failing test per kind of failure, against corpus/student (or corpus/broken)."""

import pytest
from pedagog_pytest import Rubric, Score, item, rubric, rubric_item, timeout

from linked_list import LinkedList


def filled(*values):
    items = LinkedList()
    for v in values:
        items.append(v)
    return items


def test_assert_comparing_values():
    """1. An assert comparing values (pytest explains them)."""
    assert len(filled(1, 2, 3)) == 3


def test_assert_with_a_message():
    """2. An assert with the instructor's message."""
    assert filled(1, 2).to_list() == [1, 2], "to_list() should keep insertion order"


def test_exception_in_student_code():
    """3. An exception raised in the student's code."""
    filled().pop_front()


def test_exception_in_the_test():
    """4. An exception in the test itself (a method the student didn't write)."""
    filled(1, 2).reverse()


def test_expected_exception_missing():
    """5. An expected exception that never came."""
    with pytest.raises(ValueError):
        filled(1, 2).remove(9)


class PopFront(Rubric):
    front: Score = item("returns the front", points=1)
    order: Score = item("keeps order", points=1)


@rubric(PopFront, id="pop-front", name="pop_front checks")
def test_failed_checks(scores: PopFront):
    """6. Failed check() blocks (the test goes on after each)."""
    items = filled(1, 2)
    with scores.front.check(reason="returns 1 first"):
        assert items.pop_front() == 2
    with scores.order.check(reason="the rest stays in order"):
        assert items.to_list() == [3]


@pytest.fixture
def server():
    raise ConnectionError("the course's test server is down")


def test_setup_error(server):
    """7. A fixture (setup) error."""


def test_deep_recursion():
    """8. A failure deep in the student's code (a long traceback)."""
    filled(1, 2).find(3)


@rubric_item("prints the list", points=1, id="show")
def test_wrong_output(score: Score, capsys):
    """9. Comparing printed output (under process isolation, capsys doesn't see the
    student's prints: they happen in the other process)."""
    filled(1, 2, 3).show()
    assert capsys.readouterr().out == "1 -> 2 -> 3\n"
    score.award()


@timeout(1)
def test_timeout():
    """10. A test that runs past its time limit."""
    filled(1).spin()
