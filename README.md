# pedagog-pytest

pytest helper for [pedagog](https://github.com/pedagog-org/pedagog) assignments: test
metadata decorators, per-test time limits, and pedagog's standard `results.yaml`.

Assignments select it with `testing: { framework: pytest:<version> }`; pedagog installs it
into the assignment image and runs the hidden tests with it.

## Writing tests

```python
from pedagog_pytest import AFTER_END, HIDDEN, Expect, number, solution_expect, timeout, visibility, weight

@weight(10)                      # points if it passes; default 1.0
@number("1.1")                   # label and position in the results
@visibility(output=HIDDEN)       # default: score VISIBLE, output AFTER_END
@timeout(5)                      # seconds; default 10.0, 0 disables
def test_append():
    """Appends one element"""   # first docstring line is the test name
    ...

@visibility(AFTER_END)           # one value sets both score and output
@solution_expect(Expect.FAIL)    # the reference solution is meant to fail this one
def test_stretch_goal():
    ...
```

| Decorator | What it's for | Default (undecorated) |
| --- | --- | --- |
| `weight(points)` | Points the test is worth: all on pass, 0 otherwise | `1.0` |
| `number(value)` | Label shown with the test, and its position: results are sorted by number (dotted parts numerically, `1.2` before `1.10`), unnumbered tests last in collection order | none |
| `visibility(level)` / `visibility(score=, output=)` | When students see the score and the output (failure messages, which can reveal what a test checks) | score `VISIBLE`, output `AFTER_END` |
| `solution_expect(outcome)` | What the reference solution should do during validation; validation fails on a mismatch | `Expect.PASS` |
| `timeout(seconds)` | Time limit; a slower test fails (under isolation its process is killed) | `10.0` |
| `rubric_item(name, points)` / `rubric(Rubric)` | Partial credit: the test awards points itself (see Rubrics) | — |

`Visibility` values: `VISIBLE` (immediately), `HIDDEN` (never), `AFTER_END` (once the student
can no longer submit: the late deadline if late submissions are allowed, otherwise the due
date), `AFTER_PUBLISH` (when the instructor publishes results); also exported as module
constants. The package is typed (`py.typed`, checked with `mypy --strict`).

## Rubrics (partial credit)

Instead of all-or-nothing `@weight`, a test can award points itself. An item that's never
awarded scores 0 (even if the test passes), and awards stand even if the test fails
afterwards: status and score are independent.

**One item** — `@rubric_item` and the `score` fixture:

```python
from pedagog_pytest import Score, rubric_item

@rubric_item("Appends in order", points=10)   # name defaults to the test's name
def test_append(score: Score):
    score.award()                # points=FULL: all 10
    check_edge_cases()
    score.award(points=6)        # replaces the earlier award
```

**Several items** — a `Rubric` subclass (attribute names are the item ids), `@rubric`, and
the `scores` fixture, which is an instance of your rubric:

```python
from pedagog_pytest import Rubric, Score, item, rubric

class Design(Rubric):
    memory_safe: Score = item("Passes valgrind with no leaks", points=10)
    no_globals: Score = item("Avoids global state", points=5)
    recursion: Score = item("Uses recursion", points=2, extra_credit=True)

@rubric(Design)
def test_design(scores: Design):
    with scores.memory_safe.check():   # full points if the block completes;
        assert leaks() == 0            #   0 plus the failure recorded if it raises
    with scores.no_globals.check(points=3):
        assert not uses_globals()
    scores.recursion.award(points=1)   # still runs after a failed check
```

- `Score.award(points=FULL)` sets the item's points (0..its points); `Score.nil()` resets to 0.
- `Score.check(points=FULL)` is a soft assertion: a failing block doesn't stop the test, but
  the test ends as failed, listing the failed items. Time limits still stop it.
- A rubric test's maximum is the sum of its non-extra-credit items; extra credit adds to the
  score but not the maximum. In results.yaml its items appear under `items:` with their own
  score, max, `extra-credit` flag and failure output.
- `@weight`, `@rubric_item` and `@rubric` are mutually exclusive.

## Results

When `PEDAGOG_RESULTS_DIR` is set (pedagog sets it), or with `--pedagog-results PATH`, the
plugin writes `results.yaml` (as JSON, which is valid YAML):

- one entry per test: `id` (pytest node id), `name`, `status` (`pass`/`fail`/`error`/`skip`),
  `score` (full weight on pass, else 0), `max`, `output` (failure text), `visibility`, and
  optional `number` / `solution-expect`;
- a module that fails to import (e.g. broken student code) becomes an `error` entry, so a
  run never passes vacuously;
- `summary` totals the scores.

Plain `pytest` runs without either setting write nothing.

## Time limits

Each test gets 10 s by default (`--pedagog-timeout SECONDS` changes the default, `@timeout`
overrides per test). The limit raises an exception that `except Exception` in student code
can't swallow. Under process isolation the runner process enforces it as well.

## Development

Tests run in containers, no local Python needed:

```sh
podman run --rm -v "$PWD:/src:z" -w /src python:3.12-slim \
  sh -c 'pip install -q -e . && python -m pytest -q -p no:cacheprovider'
```

## License

AGPL-3.0-only.
