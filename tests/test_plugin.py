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
        from pedagog_pytest import HIDDEN, Expect, number, solution_expect, visibility, weight

        @weight(10)
        @number("2.1")
        @visibility(output=HIDDEN)
        @solution_expect(Expect.FAIL)
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


def test_one_visibility_sets_both_parts(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import AFTER_END, visibility

        @visibility(AFTER_END)
        def test_x():
            pass
        """,
    )
    assert by_id(results)["test_x"]["visibility"] == {"score": "after_end", "output": "after_end"}


def test_results_are_ordered_by_number(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import number

        def test_unnumbered_first_collected():
            pass

        @number("1.10")
        def test_ten():
            pass

        @number(2)
        def test_two():
            pass

        @number("1.2")
        def test_one_two():
            pass

        def test_unnumbered_last_collected():
            pass
        """,
    )
    order = [t["id"].split("::")[-1] for t in results["tests"]]
    assert order == [
        "test_one_two",
        "test_ten",
        "test_two",
        "test_unnumbered_first_collected",
        "test_unnumbered_last_collected",
    ]


def test_defaults_are_explicit_in_signatures():
    import inspect

    import pedagog_pytest as p

    assert inspect.signature(p.weight).parameters["points"].default == 1.0
    assert inspect.signature(p.timeout).parameters["seconds"].default == 10.0
    vis = inspect.signature(p.visibility).parameters
    assert vis["score"].default is p.Visibility.VISIBLE
    assert vis["output"].default is p.Visibility.AFTER_END


def test_mixing_a_level_with_parts_is_rejected():
    from pedagog_pytest import AFTER_END, HIDDEN, visibility

    with pytest.raises(TypeError):
        visibility(AFTER_END, output=HIDDEN)


@pytest.mark.parametrize(
    "call",
    [
        "weight(-1)",
        "visibility(score='sometimes')",
        "solution_expect('maybe')",
        "timeout(-5)",
        "number('  ')",
    ],
)
def test_invalid_decorator_arguments_are_rejected(call):
    import pedagog_pytest  # noqa: F401

    with pytest.raises(ValueError):
        eval(call, vars(pedagog_pytest))


def test_rubric_item_scores_only_what_is_awarded(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import Score, rubric_item

        @rubric_item("Appends in order", points=10)
        def test_full(score: Score):
            score.award()

        @rubric_item(points=10)
        def test_partial(score: Score):
            score.award(points=6)

        @rubric_item(points=10)
        def test_never_awarded_scores_zero_even_on_pass(score: Score):
            pass

        @rubric_item(points=10)
        def test_award_stands_after_a_later_failure(score: Score):
            score.award(points=4)
            assert False, "part two broke"

        @rubric_item(points=10)
        def test_nil_resets(score: Score):
            score.award()
            score.nil()
        """,
    )
    t = by_id(results)
    assert (t["test_full"]["name"], t["test_full"]["score"], t["test_full"]["max"]) == ("Appends in order", 10.0, 10.0)
    assert t["test_partial"]["score"] == 6.0
    assert t["test_never_awarded_scores_zero_even_on_pass"]["status"] == "pass"
    assert t["test_never_awarded_scores_zero_even_on_pass"]["score"] == 0.0
    later = t["test_award_stands_after_a_later_failure"]
    assert (later["status"], later["score"]) == ("fail", 4.0)
    assert t["test_nil_resets"]["score"] == 0.0
    assert "items" not in t["test_full"]
    assert results["summary"] == {"score": 20.0, "total": 50.0}


def test_awards_outside_the_items_points_are_rejected(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import Score, rubric_item

        @rubric_item(points=5)
        def test_too_much(score: Score):
            score.award(points=6)
        """,
    )
    t = by_id(results)["test_too_much"]
    assert t["status"] == "fail" and "outside 0..5" in t["output"]


RUBRIC = """
    from pedagog_pytest import Rubric, Score, item, rubric

    class Design(Rubric):
        memory_safe: Score = item("Passes valgrind with no leaks", points=10)
        no_globals: Score = item("Avoids global state", points=5)
        recursion: Score = item("Uses recursion", points=2, extra_credit=True)
"""


def test_rubric_items_nest_under_the_test(pytester):
    results = run(
        pytester,
        RUBRIC
        + """
    @rubric(Design)
    def test_design(scores: Design):
        scores.memory_safe.award()
        scores.no_globals.award(points=2.5)
        scores.recursion.award()
    """,
    )
    t = by_id(results)["test_design"]
    assert (t["status"], t["score"], t["max"]) == ("pass", 14.5, 15.0)
    assert t["items"] == [
        {"id": "memory_safe", "name": "Passes valgrind with no leaks", "score": 10.0, "max": 10.0, "extra-credit": False, "output": ""},
        {"id": "no_globals", "name": "Avoids global state", "score": 2.5, "max": 5.0, "extra-credit": False, "output": ""},
        {"id": "recursion", "name": "Uses recursion", "score": 2.0, "max": 2.0, "extra-credit": True, "output": ""},
    ]
    assert results["summary"] == {"score": 14.5, "total": 15.0}


def test_check_blocks_score_each_item_and_keep_going(pytester):
    results = run(
        pytester,
        RUBRIC
        + """
    @rubric(Design)
    def test_design(scores: Design):
        with scores.memory_safe.check():
            assert 1 + 1 == 3, "leaks found"
        with scores.no_globals.check(points=3):
            pass
        scores.recursion.award(points=1)   # still reached after the failed check
    """,
    )
    t = by_id(results)["test_design"]
    items = {i["id"]: i for i in t["items"]}
    assert items["memory_safe"]["score"] == 0.0
    assert "leaks found" in items["memory_safe"]["output"]
    assert items["no_globals"]["score"] == 3.0
    assert items["recursion"]["score"] == 1.0
    assert t["status"] == "fail"
    assert "rubric checks failed: memory_safe" in t["output"]
    assert t["score"] == 4.0


def test_time_limits_are_not_swallowed_by_checks(pytester):
    results = run(
        pytester,
        """
        import time
        from pedagog_pytest import Score, rubric_item, timeout

        @timeout(0.3)
        @rubric_item(points=2)
        def test_hangs_in_check(score: Score):
            with score.check():
                time.sleep(5)
        """,
    )
    t = by_id(results)["test_hangs_in_check"]
    assert t["status"] == "fail" and "time limit" in t["output"]
    assert t["score"] == 0.0


def test_conflicting_scoring_decorators_are_a_usage_error(pytester):
    pytester.makepyfile(
        test_x="""
        from pedagog_pytest import rubric_item, weight

        @weight(2)
        @rubric_item(points=3)
        def test_x(score):
            pass
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*use only one of*"])


def test_scoring_fixtures_need_their_decorator(pytester):
    results = run(pytester, "def test_x(score):\n    pass\n")
    t = by_id(results)["test_x"]
    assert t["status"] == "error" and "needs @rubric_item" in t["output"]


def test_rubric_must_declare_items():
    from pedagog_pytest import Rubric

    with pytest.raises(TypeError, match="declares no items"):

        class Empty(Rubric):
            pass
