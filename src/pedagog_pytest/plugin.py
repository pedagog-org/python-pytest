"""pytest plugin: per-test time limits, rubric scoring, and pedagog's results.yaml.

Registered through the ``pytest11`` entry point, so installing the package is
enough. Results are only written under pedagog (``PEDAGOG_RESULTS_DIR`` set) or
with ``--pedagog-results PATH``; plain local ``pytest`` runs are unaffected.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, Final

import pytest

from .scoring import Rubric, Score, describe_failure

if TYPE_CHECKING:
    from .isolation.client import StudentFinder

from .markers import (
    DEFAULT_TIMEOUT,
    DEFAULTS,
    PARTS,
    Expect,
    Visibility,
)

RESULTS_DIR_ENV: Final = "PEDAGOG_RESULTS_DIR"
# Set by the launcher: "process" runs student code in the runner process.
ISOLATION_ENV: Final = "PEDAGOG_ISOLATION"
RUNNER_SOCKET_ENV: Final = "PEDAGOG_RUNNER_SOCKET"
# Collection only imports modules; generous, but still bounded.
COLLECTION_TIMEOUT: Final = 60.0
# Keeps a runaway assertion message from bloating results.yaml.
MAX_OUTPUT_CHARS: Final = 20_000

MARKERS: Final = (
    "pedagog_number(value): label and sort position in the results",
    "pedagog_visibility(score, feedback, stdout, error): when students see each part",
    "pedagog_solution_expect(outcome): expected reference-solution outcome",
    "pedagog_timeout(seconds): per-test time limit",
    "pedagog_rubric_item(id, name, points, clamp, extra_credit): one rubric item, scored via the score fixture",
    "pedagog_rubric(cls, id, name): multi-item rubric, scored via the scores fixture",
)

SCORE_KEY: Final = pytest.StashKey[Score]()
SCORES_KEY: Final = pytest.StashKey[Rubric]()
SCORING_MARKS: Final = ("pedagog_rubric_item", "pedagog_rubric")
# The test's first failure, described for students and the grading trace.
FAILURE_KEY: Final = pytest.StashKey[str]()
# Ids of the items whose check() blocks failed (the reason the test failed).
FAILED_CHECKS_KEY: Final = pytest.StashKey[list[str]]()


class TestTimeout(BaseException):
    """Raised when a test exceeds its time limit.

    A BaseException so that student code's ``except Exception`` can't swallow it.
    """

    __test__ = False  # not a test class, despite the name


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pedagog")
    group.addoption(
        "--pedagog-results",
        metavar="PATH",
        default=None,
        help=f"write results.yaml to PATH (default: ${RESULTS_DIR_ENV}/results.yaml when set)",
    )
    group.addoption(
        "--pedagog-timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"default per-test time limit (default {DEFAULT_TIMEOUT:g}; 0 disables)",
    )


def pytest_configure(config: pytest.Config) -> None:
    for line in MARKERS:
        config.addinivalue_line("markers", line)
    path: str | None = config.getoption("--pedagog-results")
    if path is None and os.environ.get(RESULTS_DIR_ENV):
        path = os.path.join(os.environ[RESULTS_DIR_ENV], "results.yaml")
    if path is not None:
        config.pluginmanager.register(ResultsRecorder(Path(path)), "pedagog-results")
    if os.environ.get(ISOLATION_ENV) == "process":
        socket_path = os.environ.get(RUNNER_SOCKET_ENV)
        if not socket_path:
            raise pytest.UsageError(f"{ISOLATION_ENV}=process requires {RUNNER_SOCKET_ENV}")
        config.pluginmanager.register(IsolationPlugin(socket_path), "pedagog-isolation")


def _mark_arg(item: pytest.Item, name: str, default: Any) -> Any:
    mark = item.get_closest_marker(name)
    return mark.args[0] if mark is not None and mark.args else default


def _timeout_for(item: pytest.Item) -> float:
    return float(_mark_arg(item, "pedagog_timeout", item.config.getoption("--pedagog-timeout")))


def _scoring_mark(item: pytest.Item) -> pytest.Mark | None:
    return item.get_closest_marker("pedagog_rubric_item") or item.get_closest_marker("pedagog_rubric")


def _declared_id(item: pytest.Item) -> str | None:
    """The test's stable id, if declared; parametrized cases get ``id[param]``."""
    mark = _scoring_mark(item)
    declared: str | None = mark.kwargs.get("id") if mark is not None else None
    callspec = getattr(item, "callspec", None)
    if declared is not None and callspec is not None:
        return f"{declared}[{callspec.id}]"
    return declared


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    seen: dict[str, str] = {}
    for item in items:
        used = [m for m in SCORING_MARKS if item.get_closest_marker(m) is not None]
        if len(used) > 1:
            names = ", ".join("@" + m.removeprefix("pedagog_") for m in used)
            raise pytest.UsageError(f"{item.nodeid}: use only one of {names}")
        declared = _declared_id(item)
        if declared is not None:
            if declared in seen:
                raise pytest.UsageError(f"{item.nodeid}: id {declared!r} is already used by {seen[declared]}")
            seen[declared] = item.nodeid


def _new_scores(item: pytest.Item) -> tuple[Score | None, Rubric | None]:
    single = item.get_closest_marker("pedagog_rubric_item")
    if single is not None:
        kw = single.kwargs
        name = kw["name"] or _display_name(item)
        found = Score(_declared_id(item) or item.name, name, kw["points"], kw["clamp"], kw["extra_credit"])
        found.root = item.config.rootpath
        return found, None
    multi = item.get_closest_marker("pedagog_rubric")
    if multi is not None:
        rubric: Rubric = multi.args[0]()
        for s in rubric.items():
            s.root = item.config.rootpath
        return None, rubric
    return None, None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    single, multi = _new_scores(item)
    if single is not None:
        item.stash[SCORE_KEY] = single
    if multi is not None:
        item.stash[SCORES_KEY] = multi


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    excinfo = call.excinfo
    if (
        excinfo is not None
        and FAILURE_KEY not in item.stash
        and FAILED_CHECKS_KEY not in item.stash
        and not excinfo.errisinstance(pytest.skip.Exception)
    ):
        item.stash[FAILURE_KEY] = describe_failure(excinfo.value, item.config.rootpath)
    return report


@pytest.fixture
def score(request: pytest.FixtureRequest) -> Score:
    """The test's rubric item (needs ``@rubric_item``)."""
    found: Score | None = request.node.stash.get(SCORE_KEY, None)
    if found is None:
        pytest.fail("the `score` fixture needs @rubric_item on the test", pytrace=False)
    return found


@pytest.fixture
def scores(request: pytest.FixtureRequest) -> Rubric:
    """The test's rubric instance (needs ``@rubric(YourRubric)``)."""
    found: Rubric | None = request.node.stash.get(SCORES_KEY, None)
    if found is None:
        pytest.fail("the `scores` fixture needs @rubric(...) on the test", pytrace=False)
    return found


def _item_scores(item: pytest.Item) -> list[Score]:
    single = item.stash.get(SCORE_KEY, None)
    multi = item.stash.get(SCORES_KEY, None)
    return [single] if single is not None else multi.items() if multi is not None else []


def _fail_on_failed_checks(item: pytest.Item) -> None:
    failed = [s.id for s in _item_scores(item) if s.failed_checks]
    if failed:
        item.stash[FAILED_CHECKS_KEY] = failed
        pytest.fail(f"rubric checks failed: {', '.join(failed)}", pytrace=False)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    seconds = _timeout_for(item)
    # SIGALRM only works on the main thread of a Unix process; elsewhere the
    # pod deadline (and, under isolation, the runner) is the backstop.
    usable = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if seconds <= 0 or not usable:
        result = yield
        _fail_on_failed_checks(item)
        return result

    def on_alarm(signum: int, frame: FrameType | None) -> None:
        raise TestTimeout(f"test exceeded its {seconds:g}s time limit")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        result = yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    # A check() block failure doesn't stop the test, but it does fail it.
    _fail_on_failed_checks(item)
    return result


class IsolationPlugin:
    """Routes student modules to the runner: one connection for collection, then
    a fresh one (a fresh student process) per test."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.finder: StudentFinder | None = None

    def _open(self, timeout: float) -> None:
        from .isolation import client

        client.current.session = client.Session(self.socket_path, timeout)

    def _close(self) -> None:
        from .isolation import client

        if client.current.session is not None:
            client.current.session.close()
            client.current.session = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> None:
        from .isolation import client

        self._open(COLLECTION_TIMEOUT)
        assert client.current.session is not None
        self.finder = client.install(client.current.session.root.module_names())

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self._close()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self._open(_timeout_for(item))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        try:
            return (yield)
        except EOFError as e:
            # The runner child exited mid-call: its time limit, or a crash.
            raise TestTimeout(
                f"student code exceeded its {_timeout_for(item):g}s time limit "
                "or its process exited unexpectedly"
            ) from e

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_teardown(self, item: pytest.Item, nextitem: pytest.Item | None) -> None:
        self._close()

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        self._close()
        if self.finder is not None and self.finder in sys.meta_path:
            sys.meta_path.remove(self.finder)


def _num(x: float) -> str:
    return f"{x:g}"


def _sum(values: list[float]) -> str:
    return " + ".join(_num(v) for v in values).replace("+ -", "- ") or "0"


@dataclass
class _Entry:
    node: str
    name: str
    order: int
    declared_id: str | None = None
    number: str | None = None
    visibility: dict[str, Visibility] = field(default_factory=lambda: dict(DEFAULTS))
    solution_expect: Expect | None = None
    # "none": pass/fail only (0 points); "item": one rubric item; "rubric": several.
    scoring: str = "none"
    rubric_name: str | None = None
    scores: list[Score] = field(default_factory=list)
    status: str = "pass"
    # pytest's reports of the test's failures (uncaught, with values and traceback).
    reports: list[str] = field(default_factory=list)
    stdout: str = ""
    failure: str | None = None
    failed_checks: list[str] = field(default_factory=list)

    def earned(self) -> float:
        return sum(s.final for s in self.scores)

    def max(self) -> float:
        return sum(s.max for s in self.scores)

    def sort_key(self) -> tuple[int, tuple[tuple[int, int | str], ...], int]:
        """Numbered entries first, by dotted parts (numeric parts compared as
        numbers, so 1.2 < 1.10); then unnumbered ones in collection order."""
        if self.number is None:
            return (1, (), self.order)
        parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.number.split("."))
        return (0, parts, self.order)

    def error(self) -> str:
        """What went wrong: what failed checks raised, then pytest's reports."""
        failures = [f for s in self.scores for f in s.failures]
        return "\n\n".join([*failures, *self.reports])

    def status_reason(self) -> str:
        if self.status in ("pass", "skip"):
            return self.status
        if self.failed_checks:
            return f"{self.status} (checks failed: {', '.join(self.failed_checks)})"
        if self.failure is not None:
            return f"{self.status} ({self.failure.splitlines()[0]})"
        return self.status

    def trace(self) -> str:
        """How the test's score was calculated (for instructors only)."""
        title = f"{self.declared_id or self.node} — {self.name}"
        if self.scoring == "item":
            lines = [f"{title} (rubric item: {self.scores[0].header()})"]
            lines += ["  " + line for line in self.scores[0].trace()]
        elif self.scoring == "rubric":
            lines = [f"{title} (rubric {self.rubric_name})"]
            for s in self.scores:
                lines.append(f"  {s.id}  {s.header()}")
                lines += ["    " + line for line in s.trace()]
            earned, maximum = self.earned(), self.max()
            lines.append(
                f"  test: {_sum([s.final for s in self.scores])} = {_num(earned)};  "
                f"max {_sum([s.max for s in self.scores])} = {_num(maximum)}  -> {_num(earned)} / {_num(maximum)}"
            )
        else:
            lines = [f"{title} (unscored: 0 / 0)"]
        lines.append(f"  status: {self.status_reason()}")
        return "\n".join(lines)

    def to_doc(self) -> dict[str, object]:
        doc: dict[str, object] = {}
        if self.declared_id is not None:
            doc["id"] = self.declared_id
        doc.update(
            {
                "node": self.node,
                "name": self.name,
                "status": self.status,
                "score": self.earned(),
                "max": self.max(),
            }
        )
        if self.scores:
            doc["items"] = [
                {
                    "id": s.id,
                    "name": s.name,
                    "score": s.final,
                    "max": s.points,
                    "clamp": list(s.bounds),
                    "extra-credit": s.extra_credit.value,
                    "attempted": s.attempted,
                    "steps": s.feedback_steps,
                }
                for s in self.scores
            ]
        doc.update(
            {
                "stdout": self.stdout[:MAX_OUTPUT_CHARS],
                "error": self.error()[:MAX_OUTPUT_CHARS],
                "trace": self.trace()[:MAX_OUTPUT_CHARS],
                "visibility": {part: self.visibility[part].value for part in PARTS},
            }
        )
        if self.number is not None:
            doc["number"] = self.number
        if self.solution_expect is not None:
            doc["solution-expect"] = self.solution_expect.value
        return doc


def _display_name(item: pytest.Item) -> str:
    doc = getattr(getattr(item, "obj", None), "__doc__", None)
    first_line = doc.strip().splitlines()[0].strip() if doc and doc.strip() else None
    callspec = getattr(item, "callspec", None)
    if first_line is None:
        return item.name
    return f"{first_line} [{callspec.id}]" if callspec is not None else first_line


class ResultsRecorder:
    """Builds results.yaml from collection and test reports."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, _Entry] = {}
        # Modules that couldn't be collected (e.g. student code that doesn't import).
        self.collection_errors: list[str] = []

    def _next_order(self) -> int:
        return len(self.entries)

    def pytest_itemcollected(self, item: pytest.Item) -> None:
        vis = item.get_closest_marker("pedagog_visibility")
        mark = _scoring_mark(item)
        single, multi = _new_scores(item)  # placeholders (never awarded) until the test runs
        self.entries[item.nodeid] = _Entry(
            node=item.nodeid,
            name=(mark.kwargs.get("name") if mark is not None else None) or _display_name(item),
            order=self._next_order(),
            declared_id=_declared_id(item),
            scoring="item" if single is not None else "rubric" if multi is not None else "none",
            rubric_name=type(multi).__name__ if multi is not None else None,
            scores=[single] if single is not None else multi.items() if multi is not None else [],
            number=_mark_arg(item, "pedagog_number", None),
            visibility={part: Visibility(vis.kwargs[part]) for part in PARTS} if vis else dict(DEFAULTS),
            solution_expect=_mark_arg(item, "pedagog_solution_expect", None),
        )

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # A module that fails to import (e.g. broken student code) makes the
        # run an error: its tests never ran, so it can't pass vacuously.
        if report.failed:
            self.collection_errors.append(f"{report.nodeid}:\n{report.longreprtext}")

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_teardown(self, item: pytest.Item, nextitem: pytest.Item | None) -> None:
        entry = self.entries.get(item.nodeid)
        if entry is None:
            return
        actual = _item_scores(item)
        if actual:
            entry.scores = actual
        entry.failure = item.stash.get(FAILURE_KEY, None)
        entry.failed_checks = item.stash.get(FAILED_CHECKS_KEY, [])

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        entry = self.entries.get(report.nodeid)
        if entry is None:
            return
        if report.when == "call":
            if report.failed:
                entry.status = "fail"
            elif report.skipped:
                entry.status = "skip"
        elif report.failed:  # setup or teardown
            entry.status = "error"
        elif report.skipped and report.when == "setup":
            entry.status = "skip"
        if report.failed:
            entry.reports.append(report.longreprtext)
        # Each phase's report carries everything printed so far.
        entry.stdout = report.capstdout

    def _sorted(self) -> list[_Entry]:
        return sorted(self.entries.values(), key=_Entry.sort_key)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        entries = self._sorted()
        doc = {
            "submission-id": None,
            "run-id": None,
            # Whether every test could run, and what stopped them if not (for staff).
            "run": {
                "status": "error" if self.collection_errors else "complete",
                "output": "\n\n".join(self.collection_errors)[:MAX_OUTPUT_CHARS],
            },
            "summary": {
                "score": sum(e.earned() for e in entries),
                "total": sum(e.max() for e in entries),
            },
            "tests": [e.to_doc() for e in entries],
            "artifacts": [],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # JSON is valid YAML, so no YAML dependency is needed.
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        """The grading trace in the run's log, for instructors."""
        entries = self._sorted()
        if not entries:
            return
        terminalreporter.write_sep("=", "pedagog grading")
        for entry in entries:
            terminalreporter.write_line(entry.trace())
        terminalreporter.write_line(
            f"summary: score {_sum([e.earned() for e in entries])} = {_num(sum(e.earned() for e in entries))}, "
            f"total {_sum([e.max() for e in entries])} = {_num(sum(e.max() for e in entries))}"
        )
