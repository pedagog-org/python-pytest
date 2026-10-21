"""error-sanitized: what students see of a failure (the corpus's cases, in-process;
process isolation is in test_isolation.py)."""

import json
import textwrap

import pytest

STUDENT = """
class LinkedList:
    def __init__(self):
        self.head = None

    def pop_front(self):
        return self.head.value

    def find(self, value, node=None):
        node = node or self.head
        if node.value == value:
            return node
        return self.find(value, node.next)

    def spin(self):
        while True:
            pass
"""


def layout(pytester: pytest.Pytester, student: str, source: str) -> None:
    """As the launcher lays a run out: the tests in tests/ (pytest's root), the
    student's code in code/ (on the path)."""
    code = pytester.mkdir("code")
    (code / "linked_list.py").write_text(student)
    pytester.syspathinsert(code)
    tests = pytester.mkdir("tests")
    (tests / "test_suite.py").write_text(textwrap.dedent(source).lstrip("\n"))


def run(pytester: pytest.Pytester, source: str, student: str = STUDENT) -> dict:
    layout(pytester, student, source)
    out = pytester.path / "out" / "results.yaml"
    pytester.runpytest(str(pytester.path / "tests"), "--rootdir", str(pytester.path / "tests"), "--pedagog-results", str(out))
    return json.loads(out.read_text())


def sanitized(pytester: pytest.Pytester, source: str) -> dict:
    doc = run(pytester, source)
    assert doc["sanitizes-errors"] is True
    return {t["node"].split("::")[-1]: t["error-sanitized"] for t in doc["tests"]}


def test_asserts_say_where_and_the_values_without_addresses(pytester):
    got = sanitized(
        pytester,
        """
        from linked_list import LinkedList

        def make():
            return LinkedList()

        def test_values():
            assert len([make()]) == 3

        def test_message():
            assert [2, 1] == [1, 2], "to_list() should keep insertion order"
        """,
    )
    assert got["test_values"] == (
        "test_suite.py:7: assert 1 == 3\n"
        " +  where 1 = len([<LinkedList>])"
    )
    assert got["test_message"] == (
        "test_suite.py:10: to_list() should keep insertion order\n"
        "assert [2, 1] == [1, 2]\n"
        "  At index 0 diff: 2 != 1"
    ), "no 'Use -v' advice, no blank lines"


def test_a_failure_in_student_code_points_there_with_the_chain(pytester):
    got = sanitized(
        pytester,
        """
        from linked_list import LinkedList

        def test_pop():
            LinkedList().pop_front()
        """,
    )
    assert got["test_pop"] == (
        "linked_list.py:7: AttributeError: 'NoneType' object has no attribute 'value'\n"
        "\n"
        "test_suite.py:4, in test_pop\n"
        "    LinkedList().pop_front()\n"
        "linked_list.py:7, in pop_front\n"
        "    return self.head.value"
    )


def test_runaway_recursion_folds_to_one_frame(pytester):
    got = sanitized(
        pytester,
        """
        from linked_list import LinkedList

        class Node:
            def __init__(self, value, next=None):
                self.value, self.next = value, next

        def test_find():
            items = LinkedList()
            items.head = Node(1, Node(2))
            items.find(3)
        """,
    )
    summary, chain = got["test_find"].split("\n\n")
    # Where the limit trips (and its wording) varies by Python version.
    assert summary.startswith("linked_list.py:1") and "RecursionError: maximum recursion depth exceeded" in summary
    folded = [ln for ln in chain.splitlines() if "in find (×" in ln]
    assert len(folded) == 1 and len(chain.splitlines()) < 8, chain


def test_in_the_test_only_the_summary(pytester):
    got = sanitized(
        pytester,
        """
        import pytest
        from linked_list import LinkedList

        def test_missing():
            LinkedList().reverse()

        def test_no_raise():
            with pytest.raises(ValueError):
                pass
        """,
    )
    assert got["test_missing"] == (
        "test_suite.py:5: AttributeError: 'LinkedList' object has no attribute 'reverse'"
    )
    assert got["test_no_raise"] == "test_suite.py:8: expected ValueError, but nothing was raised"


def test_failed_checks_each_say_theirs(pytester):
    got = sanitized(
        pytester,
        """
        from pedagog_pytest import Rubric, Score, item, rubric

        class R(Rubric):
            a: Score = item("a", points=1)
            b: Score = item("b", points=1)

        @rubric(R)
        def test_checks(scores: R):
            with scores.a.check():
                assert 1 == 2
            with scores.b.check():
                assert 3 == 4
        """,
    )
    assert got["test_checks"] == "test_suite.py:10: assert 1 == 2\ntest_suite.py:12: assert 3 == 4", (
        "no closing 'rubric checks failed'"
    )


def test_setup_errors_in_the_tests_arent_the_students(pytester):
    got = sanitized(
        pytester,
        """
        import pytest

        @pytest.fixture
        def server():
            raise ConnectionError("the course's test server is down")

        def test_needs_server(server):
            pass
        """,
    )
    assert got["test_needs_server"] == "The tests couldn't set up; this isn't your code."


def test_a_timeout_says_where_it_was(pytester):
    got = sanitized(
        pytester,
        """
        from pedagog_pytest import timeout
        from linked_list import LinkedList

        @timeout(1)
        def test_spin():
            LinkedList().spin()
        """,
    )
    assert got["test_spin"].startswith("linked_list.py:16: ran past its 1 s time limit\n\n"), got["test_spin"]


def test_passing_tests_have_none(pytester):
    got = sanitized(pytester, "def test_ok():\n    assert True\n")
    assert got["test_ok"] == ""


def test_a_syntax_error_in_student_code_is_the_runs(pytester):
    doc = run(
        pytester,
        "from linked_list import LinkedList\n\ndef test_a():\n    pass\n",
        student="class LinkedList:\n    def append(self, value)\n        pass\n",
    )
    run_ = doc["run"]
    assert run_["status"] == "error"
    assert run_["output-sanitized"] == (
        "linked_list.py, line 2: SyntaxError: expected ':'\n"
        "    def append(self, value)\n"
        "                           ^"
    ), run_["output-sanitized"]
