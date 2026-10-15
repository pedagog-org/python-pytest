"""pytest plugin: per-test time limits and pedagog's results.yaml.

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
from typing import Any, Final

import pytest

from .scoring import Rubric, Score
from .markers import (
    DEFAULT_OUTPUT_VISIBILITY,
    DEFAULT_SCORE_VISIBILITY,
    DEFAULT_TIMEOUT,
    DEFAULT_WEIGHT,
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
    "pedagog_weight(points): points awarded when the test passes",
    "pedagog_number(value): label and sort position in the results",
    "pedagog_visibility(score, output): when students see score/output",
    "pedagog_solution_expect(outcome): expected reference-solution outcome",
    "pedagog_timeout(seconds): per-test time limit",
    "pedagog_rubric_item(name, points): single rubric item scored via the score fixture",
    "pedagog_rubric(cls): multi-item rubric scored via the scores fixture",
)

SCORE_KEY: Final = pytest.StashKey[Score]()
SCORES_KEY: Final = pytest.StashKey[Rubric]()
SCORING_MARKS: Final = ("pedagog_weight", "pedagog_rubric_item", "pedagog_rubric")


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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        used = [m for m in SCORING_MARKS if item.get_closest_marker(m) is not None]
        if len(used) > 1:
            names = ", ".join("@" + m.removeprefix("pedagog_") for m in used)
            raise pytest.UsageError(f"{item.nodeid}: use only one of {names}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    single = item.get_closest_marker("pedagog_rubric_item")
    if single is not None:
        name = single.kwargs["name"] or _display_name(item)
        item.stash[SCORE_KEY] = Score(item.name, name, single.kwargs["points"])
    multi = item.get_closest_marker("pedagog_rubric")
    if multi is not None:
        item.stash[SCORES_KEY] = multi.args[0]()


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
    failed = [s.id for s in _item_scores(item) if s.failures]
    if failed:
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
        self.finder: object | None = None

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
            sys.meta_path.remove(self.finder)  # type: ignore[arg-type]


@dataclass
class _Entry:
    id: str
    name: str
    order: int
    max: float = DEFAULT_WEIGHT
    number: str | None = None
    score_visibility: Visibility = DEFAULT_SCORE_VISIBILITY
    output_visibility: Visibility = DEFAULT_OUTPUT_VISIBILITY
    solution_expect: Expect | None = None
    # "weight": all of max on pass; "item": one rubric item; "rubric": several.
    scoring: str = "weight"
    scores: list[Score] = field(default_factory=list)
    status: str = "pass"
    output: list[str] = field(default_factory=list)

    def earned(self) -> float:
        if self.scoring == "weight":
            return self.max if self.status == "pass" else 0.0
        return sum(s.awarded for s in self.scores)

    def sort_key(self) -> tuple[int, tuple[tuple[int, int | str], ...], int]:
        """Numbered entries first, by dotted parts (numeric parts compared as
        numbers, so 1.2 < 1.10); then unnumbered ones in collection order."""
        if self.number is None:
            return (1, (), self.order)
        parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in self.number.split("."))
        return (0, parts, self.order)

    def to_doc(self) -> dict[str, object]:
        doc: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "score": self.earned(),
            "max": self.max,
            "output": "\n".join(self.output)[:MAX_OUTPUT_CHARS],
            "visibility": {"score": self.score_visibility.value, "output": self.output_visibility.value},
        }
        if self.number is not None:
            doc["number"] = self.number
        if self.solution_expect is not None:
            doc["solution-expect"] = self.solution_expect.value
        if self.scoring == "rubric":
            doc["items"] = [
                {
                    "id": s.id,
                    "name": s.name,
                    "score": s.awarded,
                    "max": s.points,
                    "extra-credit": s.extra_credit,
                    "output": "\n".join(s.failures)[:MAX_OUTPUT_CHARS],
                }
                for s in self.scores
            ]
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
        self.collection_errors: list[_Entry] = []

    def _next_order(self) -> int:
        return len(self.entries) + len(self.collection_errors)

    def pytest_itemcollected(self, item: pytest.Item) -> None:
        vis = item.get_closest_marker("pedagog_visibility")
        single = item.get_closest_marker("pedagog_rubric_item")
        multi = item.get_closest_marker("pedagog_rubric")
        if single is not None:
            scoring, maximum = "item", float(single.kwargs["points"])
            scores = [Score(item.name, single.kwargs["name"] or _display_name(item), maximum)]
        elif multi is not None:
            scoring = "rubric"
            scores = multi.args[0]().items()  # placeholder (all 0) until the test runs
            maximum = sum(s.points for s in scores if not s.extra_credit)
        else:
            scoring, scores = "weight", []
            maximum = float(_mark_arg(item, "pedagog_weight", DEFAULT_WEIGHT))
        self.entries[item.nodeid] = _Entry(
            id=item.nodeid,
            # A single rubric item's name is the test's name in the results.
            name=scores[0].name if scoring == "item" else _display_name(item),
            order=self._next_order(),
            max=maximum,
            scoring=scoring,
            scores=scores,
            number=_mark_arg(item, "pedagog_number", None),
            score_visibility=vis.kwargs["score"] if vis else DEFAULT_SCORE_VISIBILITY,
            output_visibility=vis.kwargs["output"] if vis else DEFAULT_OUTPUT_VISIBILITY,
            solution_expect=_mark_arg(item, "pedagog_solution_expect", None),
        )

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # A module that fails to import (e.g. broken student code) must not
        # leave an empty, vacuously "passing" result.
        if report.failed:
            self.collection_errors.append(
                _Entry(
                    id=f"{report.nodeid}::<collection>",
                    name=f"Collecting {report.nodeid}",
                    order=self._next_order(),
                    max=0.0,
                    status="error",
                    output=[report.longreprtext],
                )
            )

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_teardown(self, item: pytest.Item, nextitem: pytest.Item | None) -> None:
        entry = self.entries.get(item.nodeid)
        actual = _item_scores(item)
        if entry is not None and actual:
            entry.scores = actual

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
            entry.output.append(report.longreprtext)

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        entries = sorted([*self.collection_errors, *self.entries.values()], key=_Entry.sort_key)
        doc = {
            "submission-id": None,
            "run-id": None,
            "summary": {
                "score": sum(e.earned() for e in entries),
                "total": sum(e.max for e in entries),
            },
            "tests": [e.to_doc() for e in entries],
            "artifacts": [],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # JSON is valid YAML, so no YAML dependency is needed.
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
