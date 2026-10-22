"""What student code prints, carried back to the grader.

The grader hands the runner child the write ends of two pipes (stdout, stderr)
with the connection; the child points its fds 1 and 2 at them. Here a thread
drains both, so a chatty student never blocks on a full pipe, keeping the first
``MAX_OUTPUT_BYTES`` of each. After every call into student code the grader
writes what arrived to its own ``sys.stdout`` / ``sys.stderr``, where pytest's
capture (``capsys``, ``capfd``, the report) sees it.
"""

from __future__ import annotations

import codecs
import io
import os
import selectors
import socket
import sys
import threading
from typing import TextIO

# Per stream, per test; the results keep less (the plugin's own cap).
MAX_OUTPUT_BYTES = 1024 * 1024
CUT_NOTE = "\n…(student output cut at {limit} MiB)\n"
# How long to wait for the rest after the student's process died.
FINISH_SECONDS = 0.5
_CHUNK = 65536


class _Stream:
    def __init__(self, fd: int, target: str) -> None:
        self.fd = fd
        self.target = target
        self.pending = bytearray()
        self.kept = 0
        self.cut = False
        self.noted = False
        self.closed = False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def take(self, data: bytes) -> None:
        room = MAX_OUTPUT_BYTES - self.kept
        if len(data) > room:
            data = data[: max(room, 0)]
            self.cut = True
        self.pending += data
        self.kept += len(data)


class Output:
    """The grader's ends of one connection's output pipes."""

    def __init__(self) -> None:
        out_r, self._out_w = os.pipe()
        err_r, self._err_w = os.pipe()
        self._streams = [_Stream(out_r, "stdout"), _Stream(err_r, "stderr")]
        for s in self._streams:
            os.set_blocking(s.fd, False)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def send(self, sock: socket.socket) -> None:
        """Hand the write ends to the runner child (then only it holds them, so
        its exit ends the streams)."""
        try:
            socket.send_fds(sock, [b"o"], [self._out_w, self._err_w])
        finally:
            os.close(self._out_w)
            os.close(self._err_w)
        self._thread = threading.Thread(target=self._drain, name="pedagog-student-output", daemon=True)
        self._thread.start()

    def _read(self, s: _Stream) -> None:
        """Everything available now (call with the lock held)."""
        while not s.closed:
            try:
                data = os.read(s.fd, _CHUNK)
            except BlockingIOError:
                return
            if not data:
                s.closed = True
                return
            s.take(data)

    def _drain(self) -> None:
        with selectors.DefaultSelector() as sel:
            for s in self._streams:
                sel.register(s.fd, selectors.EVENT_READ, s)
            while not self._stop.is_set() and not all(s.closed for s in self._streams):
                for key, _ in sel.select(timeout=0.1):
                    s = key.data
                    with self._lock:
                        self._read(s)
                    if s.closed:
                        sel.unregister(s.fd)

    def emit(self, out: TextIO | None = None, err: TextIO | None = None) -> None:
        """Write what has arrived to the grader's streams. Whatever the child
        wrote before replying is already in the pipes, so it's included."""
        targets = {"stdout": out or sys.stdout, "stderr": err or sys.stderr}
        with self._lock:
            chunks = []
            for s in self._streams:
                self._read(s)
                text = s.decoder.decode(bytes(s.pending), final=s.closed)
                s.pending.clear()
                if s.cut and not s.noted:
                    text += CUT_NOTE.format(limit=MAX_OUTPUT_BYTES // (1024 * 1024))
                    s.noted = True
                chunks.append((targets[s.target], text))
        for target, text in chunks:
            if text:
                target.write(text)
                target.flush()

    def finish(self, timeout: float = FINISH_SECONDS) -> None:
        """Wait (briefly) for the child's output to end, e.g. after it died."""
        if self._thread is not None:
            self._thread.join(timeout)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        for s in self._streams:
            os.close(s.fd)


def redirect(conn: socket.socket) -> bool:
    """Runner child: take the grader's pipes as fds 1 and 2, unbuffered, so
    what's printed is in the pipe before the reply (and survives ``os._exit``)."""
    _, fds, _, _ = socket.recv_fds(conn, 16, 2)
    if len(fds) != 2:
        for fd in fds:
            os.close(fd)
        return False
    for fd, target in zip(fds, (1, 2)):
        os.dup2(fd, target)
        os.close(fd)
    sys.stdout = _unbuffered(1)
    sys.stderr = _unbuffered(2)
    return True


def _unbuffered(fd: int) -> TextIO:
    return io.TextIOWrapper(
        io.FileIO(fd, "w", closefd=False), encoding="utf-8", errors="backslashreplace", write_through=True
    )
