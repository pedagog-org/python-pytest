import json

import pytest


def run(pytester: pytest.Pytester, source: str, *args: str) -> dict:
    pytester.makepyfile(test_suite=source)
    out = pytester.path / "out" / "results.yaml"
    pytester.runpytest("--pedagog-results", str(out), *args)
    return json.loads(out.read_text())


def by_id(results: dict) -> dict:
    return {t["id"].split("::")[-1]: t for t in results["tests"]}


def test_defaults_for_passing_and_failing_tests(pytester):
    results = run(
        pytester,
        """
        def test_ok():
            '''Adds two numbers'''
            assert 1 + 1 == 2

        def test_bad():
            assert 1 + 1 == 3
        """,
    )
    tests = by_id(results)
    assert tests["test_ok"]["name"] == "Adds two numbers"
    assert tests["test_ok"]["status"] == "pass"
    assert tests["test_ok"]["score"] == tests["test_ok"]["max"] == 1.0
    assert tests["test_ok"]["visibility"] == {"score": "visible", "output": "after_end"}
    assert tests["test_ok"]["output"] == ""
    assert "number" not in tests["test_ok"]
    assert "solution-expect" not in tests["test_ok"]

    assert tests["test_bad"]["name"] == "test_bad"
    assert tests["test_bad"]["status"] == "fail"
    assert tests["test_bad"]["score"] == 0.0
    assert "assert (1 + 1) == 3" in tests["test_bad"]["output"]

    assert results["summary"] == {"score": 1.0, "total": 2.0}
    assert results["submission-id"] is None and results["artifacts"] == []


def test_decorators_set_metadata(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import number, solution_expect, visibility, weight

        @weight(10)
        @number("2.1")
        @visibility(output="hidden")
        @solution_expect("fail")
        def test_meta():
            assert False
        """,
    )
    t = by_id(results)["test_meta"]
    assert t["max"] == 10.0
    assert t["number"] == "2.1"
    assert t["visibility"] == {"score": "visible", "output": "hidden"}
    assert t["solution-expect"] == "fail"


def test_setup_errors_and_skips(pytester):
    results = run(
        pytester,
        """
        import pytest

        @pytest.fixture
        def broken():
            raise RuntimeError("fixture exploded")

        def test_uses_broken(broken):
            pass

        @pytest.mark.skip(reason="not today")
        def test_skipped():
            pass
        """,
    )
    tests = by_id(results)
    assert tests["test_uses_broken"]["status"] == "error"
    assert "fixture exploded" in tests["test_uses_broken"]["output"]
    assert tests["test_skipped"]["status"] == "skip"
    assert tests["test_skipped"]["score"] == 0.0


def test_collection_error_is_recorded_not_silent(pytester):
    results = run(pytester, "import does_not_exist\n\ndef test_never():\n    pass\n")
    assert len(results["tests"]) == 1
    t = results["tests"][0]
    assert t["status"] == "error"
    assert t["id"].endswith("::<collection>")
    assert "does_not_exist" in t["output"]


def test_parametrized_tests_get_distinct_ids_and_names(pytester):
    results = run(
        pytester,
        """
        import pytest

        @pytest.mark.parametrize("n", [1, 2])
        def test_square(n):
            '''Squares'''
            assert n * n > 0
        """,
    )
    names = sorted(t["name"] for t in results["tests"])
    assert names == ["Squares [1]", "Squares [2]"]
    assert len({t["id"] for t in results["tests"]}) == 2


def test_timeout_fails_the_test_even_if_student_code_catches_exceptions(pytester):
    results = run(
        pytester,
        """
        import time
        from pedagog_pytest import timeout

        def stubborn():
            while True:
                try:
                    time.sleep(0.05)
                except Exception:
                    pass

        @timeout(0.3)
        def test_hangs():
            stubborn()

        def test_after():
            assert True
        """,
    )
    tests = by_id(results)
    assert tests["test_hangs"]["status"] == "fail"
    assert "exceeded its 0.3s time limit" in tests["test_hangs"]["output"]
    assert tests["test_after"]["status"] == "pass", "later tests still run"


def test_default_timeout_option(pytester):
    results = run(
        pytester,
        "import time\n\ndef test_slow():\n    time.sleep(2)\n",
        "--pedagog-timeout",
        "0.2",
    )
    assert by_id(results)["test_slow"]["status"] == "fail"


def test_results_dir_env(pytester, monkeypatch):
    monkeypatch.setenv("PEDAGOG_RESULTS_DIR", str(pytester.path / "env-results"))
    pytester.makepyfile(test_x="def test_x():\n    pass\n")
    pytester.runpytest()
    doc = json.loads((pytester.path / "env-results" / "results.yaml").read_text())
    assert doc["tests"][0]["status"] == "pass"


def test_nothing_written_without_env_or_option(pytester, monkeypatch):
    monkeypatch.delenv("PEDAGOG_RESULTS_DIR", raising=False)
    pytester.makepyfile(test_x="def test_x():\n    pass\n")
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)
    assert not list(pytester.path.rglob("results.yaml"))


def test_markers_are_registered(pytester):
    pytester.makepyfile(
        test_x="from pedagog_pytest import weight\n\n@weight(2)\ndef test_x():\n    pass\n"
    )
    pytester.runpytest("--strict-markers").assert_outcomes(passed=1)


@pytest.mark.parametrize(
    "call",
    [
        "weight(-1)",
        "visibility(score='sometimes')",
        "solution_expect('maybe')",
        "timeout(-5)",
    ],
)
def test_invalid_decorator_arguments_are_rejected(call):
    import pedagog_pytest  # noqa: F401

    with pytest.raises(ValueError):
        eval(call, vars(pedagog_pytest))
