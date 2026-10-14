# pedagog-pytest

pytest helper for [pedagog](https://github.com/pedagog-org/pedagog) assignments: test
metadata decorators, per-test time limits, and pedagog's standard `results.yaml`.

Assignments select it with `testing: { framework: pytest:<version> }`; pedagog installs it
into the assignment image and runs the hidden tests with it.

## Writing tests

```python
from pedagog_pytest import number, solution_expect, timeout, visibility, weight

@weight(10)                                   # points if it passes (default 1)
@number("1.1")                                # display number (optional)
@visibility(score="visible", output="hidden") # default: score visible, output after_end
@timeout(5)                                   # seconds (default 10; 0 disables)
def test_append():
    """Appends one element"""                 # first docstring line is the test name
    ...

@solution_expect("fail")                      # the reference solution should fail this one
def test_rejects_bad_input():
    ...
```

Visibility values: `visible`, `hidden`, `after_end`, `after_publish`.

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
