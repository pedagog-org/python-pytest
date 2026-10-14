"""pytest plugin: per-test time limits and pedagog's results.yaml.

Registered through the ``pytest11`` entry point, so installing the package is
enough. Results are only written under pedagog (``PEDAGOG_RESULTS_DIR`` set) or
with ``--pedagog-results PATH``; plain local ``pytest`` runs are unaffected.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

RESULTS_DIR_ENV = "PEDAGOG_RESULTS_DIR"
DEFAULT_WEIGHT = 1.0
DEFAULT_TIMEOUT = 10.0
DEFAULT_VISIBILITY = {"score": "visible", "output": "after_end"}
# Keeps a runaway assertion message from bloating results.yaml.
MAX_OUTPUT_CHARS = 20_000

MARKERS = (
    "pedagog_weight(points): points awarded when the test passes",
    "pedagog_number(value): display number",
    "pedagog_visibility(score, output): when students see score/output",
    "pedagog_solution_expect(outcome): expected reference-solution outcome",
    "pedagog_timeout(seconds): per-test time limit",
)


class TestTimeout(BaseException):
    """Raised when a test exceeds its time limit.

    A BaseException so that student code's ``except Exception`` can't swallow it.
    """


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pedagog")
    group.addoption(
        "--pedagog-results",
        metavar="PATH",
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
    path = config.getoption("--pedagog-results")
    if path is None and os.environ.get(RESULTS_DIR_ENV):
        path = os.path.join(os.environ[RESULTS_DIR_ENV], "results.yaml")
    if path is not None:
        config.pluginmanager.register(ResultsRecorder(Path(path)), "pedagog-results")


def _mark_arg(item: pytest.Item, name: str, default: object = None) -> object:
    mark = item.get_closest_marker(name)
    return mark.args[0] if mark is not None and mark.args else default


def _timeout_for(item: pytest.Item) -> float:
    return float(_mark_arg(item, "pedagog_timeout", item.config.getoption("--pedagog-timeout")))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item):
    seconds = _timeout_for(item)
    # SIGALRM only works on the main thread of a Unix process; elsewhere the
    # pod deadline (and, under isolation, the runner) is the backstop.
    usable = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if seconds <= 0 or not usable:
        return (yield)

    def on_alarm(signum, frame):
        raise TestTimeout(f"test exceeded its {seconds:g}s time limit")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return (yield)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@dataclass
class _Entry:
    id: str
    name: str
    max: float
    number: str | None
    visibility: dict
    solution_expect: str | None
    status: str = "pass"
    output: list[str] = field(default_factory=list)

    def to_doc(self) -> dict:
        score = self.max if self.status == "pass" else 0.0
        doc = {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "score": score,
            "max": self.max,
            "output": "\n".join(self.output)[:MAX_OUTPUT_CHARS],
            "visibility": self.visibility,
        }
        if self.number is not None:
            doc["number"] = self.number
        if self.solution_expect is not None:
            doc["solution-expect"] = self.solution_expect
        return doc


def _display_name(item: pytest.Item) -> str:
    doc = getattr(getattr(item, "obj", None), "__doc__", None)
    first_line = doc.strip().splitlines()[0].strip() if doc and doc.strip() else None
    callspec = getattr(item, "callspec", None)
    if first_line is None:
        return item.name
    return f"{first_line} [{callspec.id}]" if callspec is not None else first_line


def _entry_for(item: pytest.Item) -> _Entry:
    vis_mark = item.get_closest_marker("pedagog_visibility")
    visibility = dict(DEFAULT_VISIBILITY)
    if vis_mark is not None:
        visibility.update({k: v for k, v in vis_mark.kwargs.items() if v is not None})
    return _Entry(
        id=item.nodeid,
        name=_display_name(item),
        max=float(_mark_arg(item, "pedagog_weight", DEFAULT_WEIGHT)),
        number=_mark_arg(item, "pedagog_number"),
        visibility=visibility,
        solution_expect=_mark_arg(item, "pedagog_solution_expect"),
    )


class ResultsRecorder:
    """Builds results.yaml from collection and test reports."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, _Entry] = {}
        self.collection_errors: list[_Entry] = []

    def pytest_itemcollected(self, item: pytest.Item) -> None:
        self.entries[item.nodeid] = _entry_for(item)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        # A module that fails to import (e.g. broken student code) must not
        # leave an empty, vacuously "passing" result.
        if report.failed:
            self.collection_errors.append(
                _Entry(
                    id=f"{report.nodeid}::<collection>",
                    name=f"Collecting {report.nodeid}",
                    max=0.0,
                    number=None,
                    visibility=dict(DEFAULT_VISIBILITY),
                    solution_expect=None,
                    status="error",
                    output=[report.longreprtext],
                )
            )

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
        tests = [e.to_doc() for e in (*self.collection_errors, *self.entries.values())]
        doc = {
            "submission-id": None,
            "run-id": None,
            "summary": {
                "score": sum(t["score"] for t in tests),
                "total": sum(t["max"] for t in tests),
            },
            "tests": tests,
            "artifacts": [],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # JSON is valid YAML, so no YAML dependency is needed.
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
