"""Process isolation, end to end: a real runner server hosts the student code and
pytester sessions act as the grader. (Separate users are covered by the launcher
tests; here everything runs as one user.)"""

import json
import subprocess
import sys
import textwrap
import time

import pytest

LINKED_LIST = """
    class LinkedList:
        def __init__(self):
            self._items = []

        def append(self, value):
            self._items.append(value)

        def to_list(self):
            return list(self._items)

        def __len__(self):
            return len(self._items)

        def __iter__(self):
            return iter(self._items)

        def __getitem__(self, i):
            return self._items[i]

        def __eq__(self, other):
            return list(self) == list(other)
"""


@pytest.fixture
def runner(tmp_path, monkeypatch):
    procs = []

    def start(**files: str):
        code = tmp_path / "code"
        code.mkdir()
        for name, src in files.items():
            (code / f"{name}.py").write_text(textwrap.dedent(src))
        sock = tmp_path / "runner.sock"
        proc = subprocess.Popen(
            [sys.executable, "-m", "pedagog_pytest.isolation.server", "--socket", str(sock), "--code", str(code)]
        )
        procs.append(proc)
        deadline = time.monotonic() + 10
        while not sock.exists():
            assert time.monotonic() < deadline, "runner didn't start"
            time.sleep(0.05)
        monkeypatch.setenv("PEDAGOG_ISOLATION", "process")
        monkeypatch.setenv("PEDAGOG_RUNNER_SOCKET", str(sock))

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


def run(pytester, source: str) -> dict:
    pytester.makepyfile(test_suite=textwrap.dedent(source))
    out = pytester.path / "results.yaml"
    # A real separate grader process, as in production (in-process pytester
    # runs re-import modules, which breaks exception-class identity).
    pytester.runpytest_subprocess("--pedagog-results", str(out))
    tests = json.loads(out.read_text())["tests"]
    return {t["id"].split("::")[-1]: t for t in tests}


def assert_all_pass(results: dict, *expected: str) -> None:
    assert sorted(results) == sorted(expected), results
    failed = {k: v["output"] for k, v in results.items() if v["status"] != "pass"}
    assert not failed, failed


def test_tests_use_student_code_natively(pytester, runner):
    runner(linked_list=LINKED_LIST)
    results = run(
        pytester,
        """
        import pytest
        from linked_list import LinkedList

        def test_append_and_read_back():
            l = LinkedList()
            l.append(1)
            l.append("two")
            assert l.to_list() == [1, "two"]
            assert type(l.to_list()) is list  # plain data comes back as real values
            assert len(l) == 2
            assert list(l) == [1, "two"]
            assert l[1] == "two"
            assert l == [1, "two"]

        def test_student_exceptions_propagate():
            with pytest.raises(IndexError):
                LinkedList()[0]
        """,
    )
    assert_all_pass(results, "test_append_and_read_back", "test_student_exceptions_propagate")


def test_each_test_gets_a_fresh_student_process(pytester, runner):
    runner(counter="count = 0\n\ndef bump():\n    global count\n    count += 1\n    return count\n")
    results = run(
        pytester,
        """
        from counter import bump

        def test_first():
            assert bump() == 1

        def test_second():
            assert bump() == 1
        """,
    )
    assert_all_pass(results, "test_first", "test_second")


def test_arguments_cross_as_copies(pytester, runner):
    runner(mutate="def grab(xs):\n    xs.append('student')\n    return type(xs).__name__\n")
    results = run(
        pytester,
        """
        from mutate import grab

        def test_copy():
            mine = [1, 2]
            assert grab(mine) == "list"
            assert mine == [1, 2]
        """,
    )
    assert_all_pass(results, "test_copy")


def test_live_objects_cannot_be_passed(pytester, runner):
    runner(apply="def call(f):\n    return f()\n")
    results = run(
        pytester,
        """
        import pytest
        from pedagog_pytest.isolation.client import IsolationError
        from apply import call

        def test_callback_refused():
            with pytest.raises(IsolationError, match="in-process"):
                call(lambda: 1)
        """,
    )
    assert_all_pass(results, "test_callback_refused")


def test_student_code_cannot_read_grader_data(pytester, runner):
    # The grader holds a secret; student code finds its rpyc connection and tries
    # every route through it. Passing means the secret never reaches the student.
    runner(
        escape="""
        import gc
        import rpyc

        def attempt():
            conns = [o for o in gc.get_objects() if isinstance(o, rpyc.Connection)]
            assert conns, "expected an rpyc connection in the student process"
            conn = conns[0]
            root = conn.root
            seen = []
            for probe in (
                lambda: root.get_service_name(),
                lambda: root._conn,
                lambda: root.exposed_anything,
                lambda: repr(root),
                lambda: dir(root),
                lambda: root(),
                lambda: conn.sync_request(rpyc.core.consts.HANDLE_GETATTR, root, "__dict__"),
            ):
                try:
                    seen.append(repr(probe()))
                except Exception as e:
                    seen.append(repr(e))
            return "\\n".join(seen)
        """
    )
    results = run(
        pytester,
        """
        from escape import attempt

        GRADER_SECRET = "s3cr3t-" + "grader-only"

        def test_secret_stays_in_the_grader():
            transcript = attempt()
            assert GRADER_SECRET not in transcript
            assert "grader-only" not in transcript
        """,
    )
    assert_all_pass(results, "test_secret_stays_in_the_grader")


def test_time_limit_kills_only_that_tests_process(pytester, runner):
    runner(spin="def forever():\n    while True:\n        pass\n\ndef ok():\n    return 1\n")
    results = run(
        pytester,
        """
        from pedagog_pytest import timeout
        from spin import forever, ok

        @timeout(0.5)
        def test_hangs():
            forever()

        def test_next_one_is_fine():
            assert ok() == 1
        """,
    )
    assert results["test_hangs"]["status"] == "fail"
    assert "time limit" in results["test_hangs"]["output"]
    assert results["test_next_one_is_fine"]["status"] == "pass"


def test_student_process_crash_fails_only_that_test(pytester, runner):
    runner(crash="import os\n\ndef die():\n    os._exit(3)\n\ndef ok():\n    return 1\n")
    results = run(
        pytester,
        """
        from crash import die, ok

        def test_dies():
            die()

        def test_survives():
            assert ok() == 1
        """,
    )
    assert results["test_dies"]["status"] == "fail"
    assert "exited unexpectedly" in results["test_dies"]["output"]
    assert results["test_survives"]["status"] == "pass"


def test_missing_student_name_is_a_collection_error(pytester, runner):
    runner(linked_list=LINKED_LIST)
    pytester.makepyfile(test_suite="from linked_list import Missing\n\ndef test_x():\n    pass\n")
    out = pytester.path / "results.yaml"
    pytester.runpytest_subprocess("--pedagog-results", str(out))
    tests = json.loads(out.read_text())["tests"]
    assert tests[0]["status"] == "error"
    assert "Missing" in tests[0]["output"]


def test_module_constants_are_copied(pytester, runner):
    runner(config="LIMIT = 5\nNAMES = ['a', 'b']\n")
    results = run(
        pytester,
        """
        from config import LIMIT, NAMES

        def test_constants():
            assert LIMIT == 5 and NAMES == ["a", "b"]
        """,
    )
    assert_all_pass(results, "test_constants")


def test_objects_do_not_outlive_their_test(pytester, runner):
    runner(linked_list=LINKED_LIST)
    results = run(
        pytester,
        """
        import pytest
        from pedagog_pytest.isolation.client import IsolationError
        from linked_list import LinkedList

        kept = []

        def test_make():
            kept.append(LinkedList())

        def test_reuse():
            with pytest.raises(IsolationError, match="earlier test"):
                kept[0].append(1)
        """,
    )
    assert_all_pass(results, "test_make", "test_reuse")
