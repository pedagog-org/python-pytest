import json

import pytest


def run(pytester: pytest.Pytester, source: str, *args: str) -> dict:
    pytester.makepyfile(test_suite=source)
    out = pytester.path / "out" / "results.yaml"
    pytester.runpytest("--pedagog-results", str(out), *args)
    return json.loads(out.read_text())


def by_id(results: dict) -> dict:
    return {t["node"].split("::")[-1]: t for t in results["tests"]}


def test_defaults_for_passing_and_failing_tests(pytester):
    results = run(
        pytester,
        """
        def test_ok():
            '''Adds two numbers'''
            print("1 + 1 is", 1 + 1)
            assert 1 + 1 == 2

        def test_bad():
            assert 1 + 1 == 3
        """,
    )
    tests = by_id(results)
    assert tests["test_ok"]["name"] == "Adds two numbers"
    assert tests["test_ok"]["status"] == "pass"
    # Undecorated tests are pass/fail only.
    assert tests["test_ok"]["score"] == tests["test_ok"]["max"] == 0.0
    assert tests["test_ok"]["visibility"] == {
        "score": "visible", "feedback": "visible", "stdout": "visible", "error": "after_end"
    }, "the error can reveal what a test expects"
    assert tests["test_ok"]["error"] == ""
    assert tests["test_ok"]["stdout"] == "1 + 1 is 2\n", "what the test printed"
    assert "number" not in tests["test_ok"]
    assert "solution-expect" not in tests["test_ok"]
    assert "id" not in tests["test_ok"] and "items" not in tests["test_ok"]
    assert "feedback" not in tests["test_ok"] and "output" not in tests["test_ok"]
    assert "(unscored: 0 / 0)" in tests["test_ok"]["trace"]

    assert tests["test_bad"]["name"] == "test_bad"
    assert tests["test_bad"]["status"] == "fail"
    assert tests["test_bad"]["score"] == 0.0
    assert "assert (1 + 1) == 3" in tests["test_bad"]["error"]

    assert results["run"] == {"status": "complete", "output": ""}
    assert results["summary"] == {"score": 0.0, "total": 0.0}
    assert results["submission-id"] is None and results["artifacts"] == []


def test_decorators_set_metadata(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import HIDDEN, Expect, number, rubric_item, solution_expect, visibility

        @rubric_item(points=10)
        @number("2.1")
        @visibility(error=HIDDEN)
        @solution_expect(Expect.FAIL)
        def test_meta(score):
            assert False
        """,
    )
    t = by_id(results)["test_meta"]
    assert t["max"] == 10.0
    assert t["number"] == "2.1"
    assert t["visibility"] == {"score": "visible", "feedback": "visible", "stdout": "visible", "error": "hidden"}
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
    assert "fixture exploded" in tests["test_uses_broken"]["error"]
    assert tests["test_skipped"]["status"] == "skip"
    assert tests["test_skipped"]["score"] == 0.0


def test_collection_error_makes_the_run_an_error(pytester):
    results = run(pytester, "import does_not_exist\n\ndef test_never():\n    pass\n")
    assert results["tests"] == []
    assert results["run"]["status"] == "error", "a run that couldn't load its tests never passes"
    assert "does_not_exist" in results["run"]["output"]


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
    assert len({t["node"] for t in results["tests"]}) == 2


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
    assert "exceeded its 0.3s time limit" in tests["test_hangs"]["error"]
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
        test_x="from pedagog_pytest import rubric_item\n\n@rubric_item(points=2)\ndef test_x():\n    pass\n"
    )
    pytester.runpytest("--strict-markers").assert_outcomes(passed=1)


def test_one_visibility_sets_every_part(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import AFTER_END, visibility

        @visibility(AFTER_END)
        def test_x():
            pass
        """,
    )
    assert by_id(results)["test_x"]["visibility"] == dict.fromkeys(("score", "feedback", "stdout", "error"), "after_end")


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
    order = [t["node"].split("::")[-1] for t in results["tests"]]
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

    item_params = inspect.signature(p.rubric_item).parameters
    assert item_params["points"].default == 0.0
    assert item_params["clamp"].default == (0.0, p.FULL)
    assert item_params["extra_credit"].default is p.ExtraCredit.NONE
    assert inspect.signature(p.item).parameters["points"].default == 0.0
    assert inspect.signature(p.timeout).parameters["seconds"].default == 10.0
    vis = inspect.signature(p.visibility).parameters
    for part in ("score", "feedback", "stdout"):
        assert vis[part].default is p.Visibility.VISIBLE
    assert vis["error"].default is p.Visibility.AFTER_END


def test_mixing_a_level_with_parts_is_rejected():
    from pedagog_pytest import AFTER_END, HIDDEN, visibility

    with pytest.raises(TypeError):
        visibility(AFTER_END, error=HIDDEN)


@pytest.mark.parametrize(
    "call",
    [
        "rubric_item(points=-1)",
        "rubric_item(clamp=(3, 1))",
        "rubric_item(id='  ')",
        "item(points=float('nan'))",
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
        def test_awards_add_up(score: Score):
            score.award(4)
            score.award(2)

        @rubric_item(points=10)
        def test_never_awarded_scores_zero_even_on_pass(score: Score):
            pass

        @rubric_item(points=10)
        def test_award_stands_after_a_later_failure(score: Score):
            score.award(4)
            assert False, "part two broke"

        @rubric_item(points=10)
        def test_full_adds_the_rest(score: Score):
            score.award(3)
            score.award()

        @rubric_item(points=10)
        def test_set_replaces(score: Score):
            score.award(9)
            score.set(2.5)

        @rubric_item(points=10)
        def test_nil_resets(score: Score):
            score.award()
            score.nil()
        """,
    )
    t = by_id(results)
    assert (t["test_full"]["name"], t["test_full"]["score"], t["test_full"]["max"]) == ("Appends in order", 10.0, 10.0)
    assert t["test_awards_add_up"]["score"] == 6.0
    assert t["test_never_awarded_scores_zero_even_on_pass"]["status"] == "pass"
    assert t["test_never_awarded_scores_zero_even_on_pass"]["score"] == 0.0
    later = t["test_award_stands_after_a_later_failure"]
    assert (later["status"], later["score"]) == ("fail", 4.0)
    assert t["test_full_adds_the_rest"]["score"] == 10.0
    assert t["test_set_replaces"]["score"] == 2.5
    assert t["test_nil_resets"]["score"] == 0.0
    assert len(t["test_full"]["items"]) == 1, "a single rubric item is the test's one item"
    assert t["test_full"]["items"][0]["clamp"] == [0.0, 10.0]
    assert results["summary"] == {"score": 32.5, "total": 70.0}


def test_clamp_applies_once_to_the_final_total(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import FULL, Score, rubric_item

        @rubric_item(points=4)
        def test_deduct_then_award(score: Score):
            score.award(-2)
            score.award(3)

        @rubric_item(points=4)
        def test_award_then_deduct(score: Score):
            score.award(3)
            score.award(-2)

        @rubric_item(points=4)
        def test_capped_at_the_max(score: Score):
            score.award(3)
            score.award(3)

        @rubric_item(points=4)
        def test_floored_at_zero(score: Score):
            score.award(-5)

        @rubric_item(points=2, clamp=None)
        def test_unbounded(score: Score):
            score.award(5)

        @rubric_item(clamp=(None, 0))
        def test_penalty(score: Score):
            score.award(-3)

        @rubric_item(points=2, clamp=(0, None))
        def test_no_cap(score: Score):
            score.award(3.5)

        @rubric_item(points=4, clamp=(1, FULL))
        def test_custom_floor(score: Score):
            pass
        """,
    )
    t = by_id(results)
    assert t["test_deduct_then_award"]["score"] == t["test_award_then_deduct"]["score"] == 1.0
    assert t["test_capped_at_the_max"]["score"] == 4.0
    assert "final 6 -> clamped to 4" in t["test_capped_at_the_max"]["trace"]
    assert t["test_floored_at_zero"]["score"] == 0.0
    assert (t["test_unbounded"]["score"], t["test_unbounded"]["items"][0]["clamp"]) == (5.0, [None, None])
    penalty = t["test_penalty"]
    assert (penalty["score"], penalty["max"], penalty["items"][0]["clamp"]) == (-3.0, 0.0, [None, 0.0])
    assert t["test_no_cap"]["score"] == 3.5
    assert t["test_custom_floor"]["score"] == 1.0


def test_extra_credit_modes(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import ExtraCredit, Rubric, Score, item, rubric

        class Stretch(Rubric):
            core: Score = item("core", points=4)
            bonus: Score = item("bonus", points=2, extra_credit=ExtraCredit.BONUS)
            earned: Score = item("earned", points=2, extra_credit=ExtraCredit.ATTEMPTED_WORK)
            tried: Score = item("tried", points=2, extra_credit=ExtraCredit.ATTEMPTED_WORK)
            skipped: Score = item("skipped", points=2, extra_credit=ExtraCredit.ATTEMPTED_WORK)
            reset: Score = item("reset", points=2, extra_credit=ExtraCredit.ATTEMPTED_WORK)

        @rubric(Stretch)
        def test_stretch(scores: Stretch):
            scores.core.award(3)
            scores.bonus.award(2)
            scores.earned.award(1)       # points imply attempted
            scores.tried.attempt()       # attempted, no points
            scores.reset.attempt()
            scores.reset.nil()           # nil() clears attempted too
        """,
    )
    t = by_id(results)["test_stretch"]
    items = {i["id"]: i for i in t["items"]}
    assert [items[k]["attempted"] for k in ("earned", "tried", "skipped", "reset")] == [True, True, False, False]
    assert items["bonus"]["extra-credit"] == "bonus"
    assert items["earned"]["extra-credit"] == "attempted-work"
    # score 3 + 2 + 1 + 0; max core 4 + earned 2 + tried 2 (bonus and unattempted don't count)
    assert (t["score"], t["max"]) == (6.0, 8.0)
    assert "not attempted: not in max" in t["trace"]
    assert "bonus: not in max" in t["trace"]


RUBRIC = """
    import pytest
    from pedagog_pytest import ExtraCredit, Rubric, Score, item, rubric

    class Design(Rubric):
        memory_safe: Score = item("Passes valgrind with no leaks", points=10)
        no_globals: Score = item("Avoids global state", points=5)
        recursion: Score = item(id="uses-recursion", name="Uses recursion", points=2, extra_credit=ExtraCredit.BONUS)
"""


def test_rubric_items_nest_under_the_test(pytester):
    results = run(
        pytester,
        RUBRIC
        + """
    @rubric(Design, id="design", name="Program design")
    def test_design(scores: Design):
        scores.memory_safe.award()
        scores.no_globals.award(2.5, reason="one global left")
        scores.recursion.award()
    """,
    )
    t = by_id(results)["test_design"]
    assert (t["id"], t["name"], t["status"], t["score"], t["max"]) == ("design", "Program design", "pass", 14.5, 15.0)
    assert t["items"] == [
        {"id": "memory_safe", "name": "Passes valgrind with no leaks", "score": 10.0, "max": 10.0,
         "clamp": [0.0, 10.0], "extra-credit": "none", "attempted": True, "steps": []},
        {"id": "no_globals", "name": "Avoids global state", "score": 2.5, "max": 5.0,
         "clamp": [0.0, 5.0], "extra-credit": "none", "attempted": True,
         "steps": [{"points": 2.5, "reason": "one global left"}]},
        {"id": "uses-recursion", "name": "Uses recursion", "score": 2.0, "max": 2.0,
         "clamp": [0.0, 2.0], "extra-credit": "bonus", "attempted": True, "steps": []},
    ]
    assert results["summary"] == {"score": 14.5, "total": 15.0}


def test_check_blocks_add_up_and_keep_going(pytester):
    results = run(
        pytester,
        RUBRIC
        + """
    @rubric(Design)
    def test_design(scores: Design):
        with scores.memory_safe.check(points=4, reason="no leaks on small input"):
            pass
        with scores.memory_safe.check(points=6, reason="no leaks on large input"):
            assert 1 + 1 == 3, "leaks found"
        with scores.no_globals.check(reason="raises on bad input"):
            with pytest.raises(ValueError):
                pass
        scores.recursion.award(1)   # still reached after the failed checks
    """,
    )
    t = by_id(results)["test_design"]
    items = {i["id"]: i for i in t["items"]}
    assert items["memory_safe"]["score"] == 4.0, "a failed check doesn't wipe earlier points"
    assert items["memory_safe"]["steps"] == [
        {"result": "pass", "points": 4.0, "worth": 4.0, "reason": "no leaks on small input"},
        {"result": "fail", "points": 0.0, "worth": 6.0, "reason": "no leaks on large input", "message": "leaks found"},
    ], "the assert's message, not pytest's values"
    first = t["error"].split("\n\n")[0]
    assert first.startswith("test_suite.py:") and "AssertionError: leaks found" in first
    assert "pedagog_pytest" not in first
    # pytest.raises' "DID NOT RAISE" is a check failure too, not an aborted test.
    assert items["no_globals"]["score"] == 0.0
    assert items["no_globals"]["steps"] == [{"result": "fail", "points": 0.0, "worth": 5.0, "reason": "raises on bad input"}]
    assert "DID NOT RAISE" in t["error"]
    assert items["uses-recursion"]["score"] == 1.0
    assert t["status"] == "fail"
    assert "rubric checks failed: memory_safe, no_globals" in t["error"]
    assert "status: fail (checks failed: memory_safe, no_globals)" in t["trace"]
    assert t["score"] == 5.0


def test_single_item_feedback_and_uncaught_failures(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import Score, rubric_item

        @rubric_item(points=2)
        def test_len(score: Score):
            score.award(1, reason="empty list")
            with score.check(points=0.5, reason="one item"):
                assert len([1]) == 2
            assert len([1, 2, 3]) == 2
            score.award(1, reason="three items")
        """,
    )
    t = by_id(results)["test_len"]
    assert t["score"] == 1.0 and t["status"] == "fail"
    assert t["items"][0]["steps"] == [
        {"points": 1.0, "reason": "empty list"},
        {"result": "fail", "points": 0.0, "worth": 0.5, "reason": "one item"},
    ], "no assert message: pytest's values stay in the error"
    assert "AssertionError: assert 1 == 2" in t["error"], "the failed check"
    assert "assert 3 == 2" in t["error"] and "where 3 = len([1, 2, 3])" in t["error"], "the uncaught failure"
    assert "status: fail (test_suite.py:" in t["trace"]


def test_grading_trace_explains_the_score(pytester):
    pytester.makepyfile(
        test_suite=RUBRIC
        + """
    @rubric(Design, id="design")
    def test_design(scores: Design):
        with scores.memory_safe.check(reason="no leaks"):
            pass
        scores.no_globals.award(-1, reason="one global")
    """
    )
    out = pytester.path / "results.yaml"
    result = pytester.runpytest("--pedagog-results", str(out))
    trace = json.loads(out.read_text())["tests"][0]["trace"]
    assert trace.splitlines() == [
        "design — test_design (rubric Design)",
        "  memory_safe  points 10, clamp [0, 10], none",
        "    check ✓ +10  no leaks  = 10",
        "    final 10  -> 10 / 10",
        "  no_globals  points 5, clamp [0, 5], none",
        "    award -1  one global  = -1",
        "    final -1 -> clamped to 0  -> 0 / 5",
        "  uses-recursion  points 2, clamp [0, 2], bonus",
        "    (never awarded)",
        "    final 0; bonus: not in max  -> 0 / 0",
        "  test: 10 + 0 + 0 = 10;  max 10 + 5 + 0 = 15  -> 10 / 15",
        "  status: pass",
    ]
    # The same trace is in the run's log, with the summary.
    result.stdout.fnmatch_lines(["*pedagog grading*", "design — test_design (rubric Design)", "summary: score 10 = 10, total 15 = 15"])


def test_declared_ids(pytester):
    results = run(
        pytester,
        """
        import pytest
        from pedagog_pytest import rubric_item

        @rubric_item(id="len", points=1)
        def test_len(score):
            score.award()

        @pytest.mark.parametrize("n", [1, 2])
        @rubric_item(id="square", points=1)
        def test_square(score, n):
            score.award()
        """,
    )
    ids = sorted(t["id"] for t in results["tests"])
    assert ids == ["len", "square[1]", "square[2]"]


def test_duplicate_declared_ids_are_a_usage_error(pytester):
    pytester.makepyfile(
        test_x="""
        from pedagog_pytest import rubric_item

        @rubric_item(id="same")
        def test_a(score):
            pass

        @rubric_item(id="same")
        def test_b(score):
            pass
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*id 'same' is already used by*"])


def test_duplicate_item_ids_are_rejected():
    from pedagog_pytest import Rubric, Score, item

    with pytest.raises(TypeError, match="duplicate item ids: a"):

        class Twice(Rubric):
            a: Score = item(points=1)
            b: Score = item(id="a", points=1)


def test_items_default_their_name_to_their_id():
    from pedagog_pytest import Rubric, Score, item

    class Named(Rubric):
        plain: Score = item(points=1)

    assert Named().items()[0].name == "plain"


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
    assert t["status"] == "fail" and "time limit" in t["error"]
    assert t["score"] == 0.0


def test_conflicting_scoring_decorators_are_a_usage_error(pytester):
    pytester.makepyfile(
        test_x="""
        from pedagog_pytest import Rubric, Score, item, rubric, rubric_item

        class R(Rubric):
            a: Score = item(points=1)

        @rubric(R)
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
    assert t["status"] == "error" and "needs @rubric_item" in t["error"]


def test_rubric_must_declare_items():
    from pedagog_pytest import Rubric

    with pytest.raises(TypeError, match="declares no items"):

        class Empty(Rubric):
            pass


def test_steps_take_ids_unique_within_their_item(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import Score, rubric_item

        @rubric_item(points=3)
        def test_ok(score: Score):
            with score.check(points=1, reason="empty", id="empty"):
                pass
            score.award(2, reason="style", id="style")

        @rubric_item(points=2)
        def test_twice(score: Score):
            with score.check(points=1, id="same"):
                pass
            with score.check(points=1, id="same"):
                pass
        """,
    )
    t = by_id(results)
    assert [s.get("id") for s in t["test_ok"]["items"][0]["steps"]] == ["empty", "style"]
    assert t["test_twice"]["status"] == "fail"
    assert "step id 'same' is already used in item" in t["test_twice"]["error"]


def test_assert_messages_from_student_code_are_not_the_instructors(pytester):
    pytester.makepyfile(student="def check_sorted(xs):\n    assert xs == sorted(xs), 'mine'\n")
    results = run(
        pytester,
        """
        from pedagog_pytest import Score, rubric_item
        from student import check_sorted

        @rubric_item(points=2)
        def test_sorted(score: Score):
            with score.check(points=1, reason="theirs"):
                check_sorted([2, 1])
            with score.check(points=1, reason="ours"):
                assert [2, 1] == [1, 2], "sort() should order the list"
        """,
    )
    steps = by_id(results)["test_sorted"]["items"][0]["steps"]
    assert "message" not in steps[0], "raised in student code"
    assert steps[1]["message"] == "sort() should order the list"


def test_set_and_nil_stay_in_the_trace(pytester):
    results = run(
        pytester,
        """
        from pedagog_pytest import Score, rubric_item

        @rubric_item(points=4)
        def test_x(score: Score):
            score.award(3, reason="kept")
            score.nil()
            score.set(2, reason="computed")
        """,
    )
    t = by_id(results)["test_x"]
    assert t["items"][0]["steps"] == [], "nil clears; set isn't feedback"
    assert "set 2  computed" in t["trace"]
