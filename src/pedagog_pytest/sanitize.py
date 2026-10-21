"""Errors as students see them (``error-sanitized``): where and what went wrong
(the student's line when it happened in their code, else the test's), a blank
line, then the frames that led there, one line of code each. Never the test's
source listing, argument dumps, object addresses, absolute paths, or helper,
pytest, RPyC or Python internals. The raw report stays in ``error`` for staff."""

from __future__ import annotations

import builtins
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from .scoring import _suite_frame

NOT_YOUR_CODE_SETUP = "The tests couldn't set up; this isn't your code."
NOT_YOUR_CODE_TEARDOWN = "The tests couldn't clean up after themselves; this isn't your code."

# What process isolation appends to an error raised in student code.
_STUDENT_NOTE = "student code traceback:"
_NOTE_FRAME = re.compile(r'\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)')
_BOUND = re.compile(r"<bound method (?P<name>[\w.]+) of <[^<>]*>>")
_OBJECT = re.compile(r"<(?:[\w.]+\.)?(?P<cls>\w+) object at 0x[0-9a-fA-F]+>")
_LIMIT = re.compile(r"(\d+(?:\.\d+)?)s time limit")
_DID_NOT_RAISE = re.compile(r"DID NOT RAISE <class '(?:[\w.]+\.)?(?P<name>\w+)'>")


@dataclass
class Frame:
    file: str
    line: int
    func: str
    code: str
    student: bool


def _relative(filename: str, root: Path | None) -> str:
    """A path students recognize: under the tests, relative to them; student code
    relative to its folder (the launcher's ``.../code/``, else its sys.path entry)."""
    path = os.path.abspath(filename)
    if root is not None:
        rel = os.path.relpath(path, root)
        if not rel.startswith(".."):
            return rel
    marker = f"{os.sep}code{os.sep}"
    if marker in path:
        return path.split(marker, 1)[1]
    entries = sorted((os.path.abspath(p) for p in sys.path if p), key=len, reverse=True)
    for entry in entries:
        if path.startswith(entry + os.sep):
            return os.path.relpath(path, entry)
    return os.path.basename(path)


def _is_student(filename: str, root: Path | None) -> bool:
    if root is None:
        return False
    return os.path.relpath(os.path.abspath(filename), root).startswith("..")


def scrub(text: str) -> str:
    """Values without addresses (``<LinkedList>``), and without pytest's advice."""
    text = _BOUND.sub(lambda m: m["name"], text)
    text = _OBJECT.sub(lambda m: f"<{m['cls']}>", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip() and ln.strip() != "Use -v to get more diff")


def _note_frames(error: BaseException) -> list[Frame]:
    """The student's frames that process isolation attached to the error."""
    frames: list[Frame] = []
    for note in getattr(error, "__notes__", None) or ():
        text = str(note)
        if not text.startswith(_STUDENT_NOTE):
            continue
        lines = text.splitlines()[1:]
        for i, ln in enumerate(lines):
            m = _NOTE_FRAME.match(ln)
            if m is None:
                continue
            code = lines[i + 1].strip() if i + 1 < len(lines) and not _NOTE_FRAME.match(lines[i + 1]) else ""
            frames.append(Frame(m["file"], int(m["line"]), m["func"], code, True))
    return frames


def _frames(error: BaseException, root: Path | None) -> list[Frame]:
    local = [
        Frame(_relative(f.filename, root), f.lineno or 0, f.name, (f.line or "").strip(), _is_student(f.filename, root))
        for f in traceback.extract_tb(error.__traceback__)
        if _suite_frame(f.filename)
    ]
    # Raised by the helper on behalf of another (the student's process ending):
    # where it happened is in what it was raised from.
    cause = error.__cause__ or error.__context__
    if not local and cause is not None and cause is not error:
        return _frames(cause, root)
    return local + _note_frames(error)


def _kind(error: BaseException) -> str:
    # RPyC re-creates builtins as generated subclasses; name the real one.
    for cls in type(error).__mro__:
        name: str = cls.__name__
        if getattr(builtins, name, None) is cls:
            return name
    return type(error).__name__


def _what(error: BaseException) -> str | None:
    """The error in a line or few; ``None`` for one that only repeats others
    (the closing "rubric checks failed")."""
    message = str(error).strip()
    name = type(error).__name__
    if name == "TestTimeout":
        limit = _LIMIT.search(message)
        within = f"its {limit.group(1)} s time limit" if limit else "its time limit"
        if "process exited" in message:
            # The student's process ended mid-call: a time limit or a crash.
            return f"ran past {within} or stopped unexpectedly"
        return f"ran past {within}"
    if name == "Failed":
        if message.startswith("rubric checks failed:"):
            return None
        raised = _DID_NOT_RAISE.search(message)
        if raised:
            return f"expected {raised['name']}, but nothing was raised"
        return scrub(message)
    if isinstance(error, AssertionError):
        # pytest's explanation already starts with "assert" or the test's message.
        return scrub(message) or "AssertionError"
    return f"{_kind(error)}: {scrub(message)}" if message else _kind(error)


def _fold(frames: list[Frame]) -> list[str]:
    """Each frame as two lines; a frame repeated in a row is shown once (×n)."""
    out: list[str] = []
    i = 0
    while i < len(frames):
        f = frames[i]
        n = 1
        while i + n < len(frames) and (frames[i + n].file, frames[i + n].line) == (f.file, f.line):
            n += 1
        out.append(f"{f.file}:{f.line}, in {f.func}" + (f" (×{n})" if n > 1 else ""))
        if f.code:
            out.append(f"    {f.code}")
        i += n
    return out


def sanitize(error: BaseException, root: Path | None, when: str = "call") -> str | None:
    """One failure for students: ``summary``, or ``summary\\n\\nchain``; ``None``
    when it only repeats others."""
    frames = _frames(error, root)
    student = [f for f in frames if f.student]
    if when in ("setup", "teardown") and not student:
        return NOT_YOUR_CODE_SETUP if when == "setup" else NOT_YOUR_CODE_TEARDOWN
    what = _what(error)
    if what is None:
        return None
    at = student[-1] if student else frames[-1] if frames else None
    summary = f"{at.file}:{at.line}: {what}" if at else what
    if len(frames) < 2:
        return summary
    return summary + "\n\n" + "\n".join(_fold(frames))


def combine(failures: list[str]) -> str:
    """Several failures: every summary first, then each one's chain."""
    summaries, chains = [], []
    for f in failures:
        summary, _, chain = f.partition("\n\n")
        summaries.append(summary)
        if chain:
            chains.append(chain)
    return "\n".join(summaries) + ("\n\n" + "\n\n".join(chains) if chains else "")


_COLLECT_FILE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')


def sanitize_collection(report: str, root: Path | None) -> str:
    """A module that couldn't be imported (pytest's collection report): where in
    the student's code and what, with the offending line (and caret) for syntax
    errors."""
    lines = [ln[1:].rstrip() if ln.startswith("E") else None for ln in report.splitlines()]
    raised = [ln for ln in lines if ln is not None]
    last = raised[-1].strip() if raised else report.strip().splitlines()[-1]
    kind, _, message = last.partition(": ")
    located: list[tuple[int, re.Match[str]]] = []
    for i, ln in enumerate(raised):
        found = _COLLECT_FILE.search(ln)
        if found is not None:
            located.append((i, found))
    if "<locals>" in kind or "Derived" in kind:
        # RPyC's generated class: a syntax error says so with its caret block.
        kind = "SyntaxError" if located else "Error"
    if not located:
        return f"{kind}: {scrub(message)}" if message else kind
    i, m = located[-1]
    detail = [ln for ln in raised[i + 1 : -1] if ln.strip()]
    width = min((len(ln) - len(ln.lstrip()) for ln in detail), default=0)
    where = f"{_relative(m['file'], root)}, line {m['line']}"
    return "\n".join([f"{where}: {kind}: {scrub(message)}", *("    " + ln[width:] for ln in detail)])
