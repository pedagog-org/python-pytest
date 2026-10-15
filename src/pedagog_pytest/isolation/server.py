"""The runner side: hosts student code, one fresh process per connection.

Run as the ``runner`` user (the launcher drops privileges first):

    python -m pedagog_pytest.isolation.server --socket PATH --code DIR

Every grader connection is served by a forked child, so each test gets clean
student-module state and a hang is confined to (and killed with) its child.
The grader only reaches student code through ``StudentService``'s exposed
methods; nothing here ever receives a reference into the grader.
"""

from __future__ import annotations

import argparse
import ctypes
from collections.abc import Callable
from typing import Any
import importlib
import importlib.util
import operator
import os
import resource
import signal
import socket
import sys
import time
from pathlib import Path

import rpyc
from rpyc.core.stream import SocketStream
from rpyc.utils.factory import connect_stream

from .codec import NotPlain, decode, encode

# Hard cap on any child's lifetime, in case student code disarms its own alarm.
MAX_CHILD_SECONDS = 600
# How long a finished test's process gets to exit before it's killed.
RETIRE_SECONDS = 2.0
# Student code may run a few helper processes, not a fork bomb.
DEFAULT_MAX_PROCS = 64
# Exit status for a child that hit its per-test time limit.
TIMEOUT_EXIT = 124

# Operators the grader can apply to student objects (run on this side).
OPS: dict[str, Callable[..., Any]] = {
    "eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le,
    "gt": operator.gt, "ge": operator.ge,
    "add": operator.add, "sub": operator.sub, "mul": operator.mul,
    "truediv": operator.truediv, "floordiv": operator.floordiv, "mod": operator.mod,
    "pow": operator.pow, "matmul": operator.matmul,
    "and": operator.and_, "or": operator.or_, "xor": operator.xor,
    "lshift": operator.lshift, "rshift": operator.rshift,
    "neg": operator.neg, "pos": operator.pos, "abs": operator.abs, "invert": operator.invert,
    "contains": operator.contains, "getitem": operator.getitem,
    "setitem": operator.setitem, "delitem": operator.delitem,
    "len": len, "bool": bool, "str": str, "repr": repr, "hash": hash,
    "int": int, "float": float, "index": operator.index,
    "iter": iter, "next": next,
}

# The grader drives us only through StudentService's exposed methods. rpyc's
# allow_getattr is a master switch (exposed methods are looked up through it),
# so it stays on while every broader attribute permission is off.
RUNNER_CONFIG: dict[str, object] = {
    "allow_public_attrs": False,
    "allow_all_attrs": False,
    "allow_safe_attrs": False,
    "allow_exposed_attrs": True,
    "allow_getattr": True,
    "allow_setattr": False,
    "allow_delattr": False,
    "allow_pickle": False,
    "sync_request_timeout": None,
}


class StudentService(rpyc.Service):  # type: ignore[misc]  # rpyc is untyped
    """Student objects never leave this process: the grader gets integer
    handles into ``objects`` and every exposed method takes and returns only
    strings, integers and tuples of them."""

    def __init__(self, code_dir: Path) -> None:
        self.code_dir = code_dir
        self.objects: dict[int, Any] = {}

    def _describe(self, value: object) -> tuple[str, str | int]:
        try:
            return ("plain", encode(value))
        except NotPlain:
            handle = len(self.objects)
            self.objects[handle] = value
            return ("ref", handle)

    def _decode(self, payload: str) -> Any:
        return decode(payload, resolve=self.objects.__getitem__)

    def exposed_begin(self, timeout: float) -> None:
        if timeout > 0:
            signal.signal(signal.SIGALRM, lambda *_: os._exit(TIMEOUT_EXIT))
            signal.setitimer(signal.ITIMER_REAL, timeout)

    def exposed_module_names(self) -> tuple[str, ...]:
        names = []
        for entry in sorted(self.code_dir.iterdir()):
            if entry.suffix == ".py" and entry.stem.isidentifier():
                names.append(entry.stem)
            elif entry.is_dir() and entry.name.isidentifier() and (entry / "__init__.py").exists():
                names.append(entry.name)
        return tuple(names)

    def exposed_has_module(self, name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def exposed_load(self, name: str) -> tuple[str, str | int]:
        return self._describe(importlib.import_module(name))

    def exposed_hasattr(self, handle: int, name: str) -> bool:
        return hasattr(self.objects[handle], name)

    def exposed_getattr(self, handle: int, name: str) -> tuple[str, str | int]:
        return self._describe(getattr(self.objects[handle], name))

    def exposed_setattr(self, handle: int, name: str, payload: str) -> None:
        setattr(self.objects[handle], name, self._decode(payload))

    def exposed_call(self, handle: int, payload: str) -> tuple[str, str | int]:
        args, kwargs = self._decode(payload)
        return self._describe(self.objects[handle](*args, **kwargs))

    def exposed_op(self, name: str, payload: str) -> tuple[str, str | int]:
        return self._describe(OPS[name](*self._decode(payload)))


def _handle(conn: socket.socket, code_dir: Path) -> None:
    sys.path.insert(0, str(code_dir))
    os.chdir(code_dir)
    connection = connect_stream(SocketStream(conn), service=StudentService(code_dir), config=RUNNER_CONFIG)
    try:
        connection.serve_all()
    finally:
        connection.close()


PR_SET_CHILD_SUBREAPER = 36


def _become_subreaper() -> None:
    """Have orphaned student processes re-parented to this server (not the
    container's PID 1, which won't reap them), so killed ones don't linger as
    zombies that still count against the per-user process limit."""
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


def _sweep() -> None:
    """Kill every other process of this user, then reap them: lingering
    background processes a test started must not survive into the next one.
    Only safe as the dedicated runner user (kill(-1) reaches everything the
    caller may signal)."""
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        return
    # Everything killed was our descendant (we're the subreaper); collect them.
    deadline = time.monotonic() + RETIRE_SECONDS
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.01)


def _retire(children: dict[int, float]) -> None:
    """Wait briefly for earlier children (their grader connections are closed,
    so they're exiting), then kill and reap any that linger."""
    deadline = time.monotonic() + RETIRE_SECONDS
    for pid in list(children):
        while True:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done or time.monotonic() > deadline:
                break
            time.sleep(0.01)
        if not done:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        del children[pid]


def serve(socket_path: Path, code_dir: Path, sweep: bool = False) -> None:
    if socket_path.exists():
        socket_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o666)
    listener.listen(16)
    listener.settimeout(0.5)
    children: dict[int, float] = {}
    while True:
        now = time.monotonic()
        for pid, started in list(children.items()):
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                del children[pid]
            elif now - started > MAX_CHILD_SECONDS:
                os.kill(pid, signal.SIGKILL)
        try:
            conn, _ = listener.accept()
        except TimeoutError:
            continue
        if sweep:
            # Tests run one at a time: before the next one starts, make sure the
            # previous test's process is gone and nothing it started survives.
            _retire(children)
            _sweep()
        pid = os.fork()
        if pid == 0:
            listener.close()
            try:
                _handle(conn, code_dir)
            finally:
                os._exit(0)
        conn.close()
        children[pid] = now


def main() -> None:
    parser = argparse.ArgumentParser(description="pedagog student-code runner")
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--code", required=True, type=Path)
    parser.add_argument("--sweep", action="store_true",
                        help="before each test, kill this user's other processes (dedicated runner user only)")
    parser.add_argument("--max-procs", type=int, default=None,
                        help=f"per-user process limit (e.g. {DEFAULT_MAX_PROCS}); unset leaves the limit alone")
    args = parser.parse_args()
    if args.sweep and os.getuid() == 0:
        parser.error("--sweep must not run as root")
    if args.sweep:
        _become_subreaper()
    if args.max_procs is not None:
        resource.setrlimit(resource.RLIMIT_NPROC, (args.max_procs, args.max_procs))
    serve(args.socket, args.code.resolve(), sweep=args.sweep)


if __name__ == "__main__":
    main()
