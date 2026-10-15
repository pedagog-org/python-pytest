# pedagog-pytest

pytest helper for [pedagog](https://github.com/pedagog-org/pedagog) assignments: test
metadata decorators, per-test time limits, and pedagog's standard `results.yaml`.

Assignments select it with `testing: { framework: pytest:<version> }`; pedagog installs it
into the assignment image and runs the hidden tests with it.

## Writing tests

Points come only from rubric scoring: a test decorated with `@rubric_item` (one item) or
`@rubric` (several) awards them explicitly. An undecorated test is pass/fail only and
scores 0 of 0.

```python
from pedagog_pytest import AFTER_END, HIDDEN, Expect, Score, number, rubric_item, solution_expect, timeout, visibility

@rubric_item("Appends one element", points=10, id="append")   # see Rubrics
@number("1.1")                   # label and position in the results
@visibility(output=HIDDEN)       # default: score VISIBLE, output AFTER_END
@timeout(5)                      # seconds; default 10.0, 0 disables
def test_append(score: Score):
    ...
    score.award()                # all 10 points (only reached if the asserts above pass)

@visibility(AFTER_END)           # one value sets both score and output
@solution_expect(Expect.FAIL)    # the reference solution is meant to fail this one
def test_stretch_goal():
    """Reverses in place"""      # first docstring line is the test name
    ...
```

| Decorator | What it's for | Default (undecorated) |
| --- | --- | --- |
| `rubric_item(name, points, *, id, clamp, extra_credit)` | The test is one rubric item, scored through the `score` fixture | pass/fail only, 0 points |
| `rubric(Rubric, *, id, name)` | The test is scored by several items, through the `scores` fixture | pass/fail only, 0 points |
| `number(value)` | Label shown with the test, and its position: results are sorted by number (dotted parts numerically, `1.2` before `1.10`), unnumbered tests last in collection order | none |
| `visibility(level)` / `visibility(score=, output=)` | When students see the score and the output (failure messages and feedback, which can reveal what a test checks) | score `VISIBLE`, output `AFTER_END` |
| `solution_expect(outcome)` | What the reference solution should do during validation; validation fails on a mismatch | `Expect.PASS` |
| `timeout(seconds)` | Time limit; a slower test fails (under isolation its process is killed) | `10.0` |

`Visibility` values: `VISIBLE` (immediately), `HIDDEN` (never), `AFTER_END` (once the student
can no longer submit: the late deadline if late submissions are allowed, otherwise the due
date), `AFTER_PUBLISH` (when the instructor publishes results); also exported as module
constants. The package is typed (`py.typed`, checked with `mypy --strict`).

## Rubrics

An item that's never awarded scores 0, even if the test passes. Awards add up and stand even
if the test fails afterwards, so a failing assert simply stops later awards.

**One item:** `@rubric_item` and the `score` fixture.

```python
from pedagog_pytest import Score, rubric_item

@rubric_item("len counts elements", points=2, id="len")
def test_len(score: Score):
    assert len(make_list(0)) == 0
    score.award(1, reason="empty list")
    assert len(make_list(3)) == 3      # if this fails, the student keeps 1 of 2
    score.award(1, reason="three items")
```

**Several items:** a `Rubric` subclass, `@rubric`, and the `scores` fixture (an instance of
your rubric).

```python
from pedagog_pytest import ExtraCredit, Rubric, Score, item, rubric

class Sorting(Rubric):
    correct: Score = item("sorts correctly", points=4)
    stable: Score = item("keeps equal elements in order", points=1, extra_credit=ExtraCredit.BONUS)
    reverse: Score = item("sorts descending", points=2, extra_credit=ExtraCredit.ATTEMPTED_WORK)
    late: Score = item("late-file penalty", clamp=(None, 0))

@rubric(Sorting, id="sort", name="sort()")
def test_sort(scores: Sorting):
    with scores.correct.check(points=1, reason="empty list"):
        assert sort([]) == []
    with scores.correct.check(points=3, reason="1000 random values"):
        assert sort(data) == sorted(data)
    if hasattr(student, "sort_descending"):
        scores.reverse.attempt()
    ...
```

- **Scoring calls** (on any `Score`):
  - `award(points=FULL, reason=None)` adds points (negative to deduct); `FULL` adds the rest
    of the item's points.
  - `set(points, reason=None)` replaces the running total (for computed scores).
  - `nil()` resets the item: no points, no feedback, not attempted.
  - `check(points=FULL, reason=None)` adds `points` if its block completes. If the block
    raises (including `pytest.raises`' "DID NOT RAISE"), it adds nothing and records the
    failure, and the test carries on but ends as failed. Time limits still stop it.
  - `attempt()` marks an `ATTEMPTED_WORK` item attempted without points.
- **Items:** `item(name=None, points=0, *, id=None, clamp=(0, FULL), extra_credit=NONE)`.
  The id defaults to the attribute name and the name to the id; item ids must be unique.
- **Clamp** bounds the item's *final* total, so awards and deductions commute. `FULL` is the
  item's points, `None` leaves a side unbounded, and `clamp=None` leaves both.
- **Extra credit:** `NONE` counts in the score and the max; `BONUS` counts in the score
  only; `ATTEMPTED_WORK` counts in the max only once attempted (any points awarded, or
  `attempt()`).
- **Ids:** `id=` on `@rubric_item` / `@rubric` is the test's stable id (parametrized cases
  get `id[param]`); duplicates are a usage error. Tests without one have no id.
- `@rubric_item` and `@rubric` are mutually exclusive.

**Feedback** (shown to students, subject to *output* visibility) reads as a checklist:

```text
✓ +1 empty list
✗ +0 1000 random values (worth 3)
  test_sort.py:12: AssertionError: assert [3, 1, 2] == [1, 2, 3]
+1 first 10 elements correct
```

**Grading trace** (for instructors only): every test's results entry has a `trace` that
explains its score step by step, and the same trace is printed at the end of the run:

```text
sort — sort() (rubric Sorting)
  correct  points 4, clamp [0, 4], none
    check ✓ +1  empty list  = 1
    check ✗ +0  1000 random values (worth 3): AssertionError  = 1
    final 1  -> 1 / 4
  ...
  test: 1 + 0 + 0 + 0 = 1;  max 4 + 0 + 0 + 0 = 4  -> 1 / 4
  status: fail (checks failed: correct)
```

## Results

When `PEDAGOG_RESULTS_DIR` is set (pedagog sets it), or with `--pedagog-results PATH`, the
plugin writes `results.yaml` (as JSON, which is valid YAML):

- one entry per test: `id` (only if declared), `node` (pytest node id), `name`, `status`
  (`pass`/`fail`/`error`/`skip`), `score`, `max`, `output` (pytest's failure text),
  `feedback`, `trace`, `visibility`, and optional `number` / `solution-expect`;
- `@rubric_item` tests add `clamp`, `extra-credit` and `attempted`; `@rubric` tests list
  their `items` (`id`, `name`, `score`, `max`, `clamp`, `extra-credit`, `attempted`,
  `feedback`);
- a module that fails to import (e.g. broken student code) becomes an `error` entry, so a
  run never passes vacuously;
- `summary` totals the scores and maxes.

Plain `pytest` runs without either setting write nothing.

## Time limits

Each test gets 10 s by default (`--pedagog-timeout SECONDS` changes the default, `@timeout`
overrides per test). The limit raises an exception that `except Exception` in student code
can't swallow. Under process isolation the runner process enforces it as well.

## Process isolation

With `testing.isolation: process` (pedagog's default), student code never runs in the test
process. pedagog's launcher starts a runner server as the unprivileged `runner` user and runs
pytest as `grader`; the plugin routes student modules to the runner:

```python
from linked_list import LinkedList   # resolves to the student's module in the runner

def test_append():
    l = LinkedList()                 # the object lives in the runner process
    l.append(1)
    assert l.to_list() == [1]        # plain data comes back as real local values
```

Rules:

- **Fresh process per test.** Each test talks to a newly forked runner process, so student
  module state never leaks between tests, and a hang or crash only fails that test.
- **Arguments cross as copies.** Plain data (numbers, strings, bytes, lists, tuples, dicts,
  sets) is copied; functions, generators and other live objects can't be passed. Tests that
  need callbacks use `isolation: in-process`.
- **Student objects stay remote.** Tests hold proxies (attribute access, calls, operators,
  iteration, `len`, indexing all work); `isinstance`/`type()` checks against student classes
  don't, and student objects don't outlive the test that made them (so don't keep them in
  module- or session-scoped fixtures).
- **Exceptions:** builtin exceptions raised by student code propagate with their type
  (`pytest.raises(ValueError)` works); custom student exception classes arrive as a generic
  remote error (match on the message instead).

Security model: the runner holds student objects in a table and the grader only ever sees
integer handles, so rpyc's object-reference machinery is never used for student data.
Everything crossing the boundary is plain JSON (no pickle), every reply is type-checked, and
the grader's end of the connection refuses all attribute access. A hostile student process
can break its own grading, but can't read or run anything in the grader. Combined with the
`runner` user (which can't read `/pedagog/tests` or `/pedagog/solution`), student code can't
see the hidden tests.

## Releases

Tagging `vX.Y.Z` (matching `pyproject.toml`) builds `pedagog-pytest-X.Y.Z-bundle.tar.gz`, a
tarball of wheels for this package and all its pinned dependencies (pure Python), and
attaches it to the GitHub release. pedagog's `pedagog-pytest` addon recipe vends that bundle
and installs it offline:

```sh
python3 -m venv --system-site-packages /opt/pedagog/pytest
/opt/pedagog/pytest/bin/pip install --no-index --find-links wheels/ pedagog-pytest
```

`/pedagog/bin/run-tests` (the `pedagog-run-tests` entry point) is the root launcher pedagog
calls; see `src/pedagog_pytest/launcher.py`. PyPI publishing is wired but off until trusted
publishing is configured (`PYPI_PUBLISH` repository variable).

## Development

Tests run in containers, no local Python needed. The launcher tests need root and the
`grader`/`runner` users (skipped otherwise):

```sh
podman run --rm -v "$PWD:/src:z" -w /src python:3.12-slim sh -c '
  useradd -r -M -u 900 grader && useradd -r -M -u 901 runner &&
  pip install -q -e . && cp -r /src /tmp/w && cd /tmp/w &&
  python -m pytest -q -p no:cacheprovider'
```

## License

AGPL-3.0-only.
