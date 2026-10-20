"""The grader side: proxies to student objects living in the runner process.

Rules that keep the grader safe from student code:

- Student objects stay in the runner; the grader only holds integer handles.
  No rpyc object references cross in either direction, so rpyc's netref
  machinery (which trusts the peer's class names) is never used.
- Arguments are sent as plain-data JSON (``codec``); live grader objects
  (functions, generators, ...) can't be passed at all.
- Plain data returned by student code is rebuilt locally from JSON, and every
  reply is type-checked; anything malformed is refused.
- The grader's end of the connection allows no attribute access of any kind.

A hostile student process can therefore only break its own grading.
"""

from __future__ import annotations

import builtins
import importlib.abc
import importlib.machinery
import os
import re
import sys
import threading
import types
from collections.abc import Callable, Sequence
from typing import Any, cast

import rpyc
from rpyc.utils.factory import unix_connect

from .codec import NotPlain, decode, encode

GRADER_CONFIG: dict[str, object] = {
    "allow_public_attrs": False,
    "allow_all_attrs": False,
    "allow_safe_attrs": False,
    "allow_exposed_attrs": False,
    "allow_getattr": False,
    "allow_setattr": False,
    "allow_delattr": False,
    "allow_pickle": False,
    "import_custom_exceptions": False,
    "instantiate_custom_exceptions": False,
    "include_local_traceback": False,
}


class IsolationError(Exception):
    """Something can't be done across the process boundary."""


# This package, wherever it's installed (site-packages, or a source checkout).
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _student_frames(remote_tb: str) -> str:
    """The parts of a remote traceback that are student code (not rpyc or
    pedagog_pytest internals)."""
    kept: list[str] = []
    keep = False
    for line in remote_tb.splitlines():
        if line.startswith('  File "'):
            keep = not any(s in line for s in ("site-packages", "<frozen", f'"{_PACKAGE_DIR}{os.sep}'))
        elif not line.startswith("    "):
            keep = False
        if keep:
            # Paths relative to the student's code, not the launcher's scratch dir.
            kept.append(re.sub(r'File ".*?/code/', 'File "', line))
    return "\n".join(kept)


def _clean(error: Exception) -> Exception:
    """Re-create an error from student code without rpyc's wrapping: same
    type and message, plus only the student's own frames."""
    remote_tb = getattr(error, "_remote_tb", None)
    if remote_tb is None:
        return error
    args = error.args[:1] if error.args else ()
    # rpyc wraps builtin exceptions in a generated subclass; use the real type.
    # (Its __module__ claims "builtins", so match the actual builtin object.)
    kind = next((c for c in type(error).__mro__ if getattr(builtins, c.__name__, None) is c), Exception)
    try:
        clean = kind(*args)
    except Exception:
        return error
    # Raised in student code: whatever message it carries isn't the instructor's.
    setattr(clean, "_pedagog_from_student", True)
    frames = _student_frames(remote_tb if isinstance(remote_tb, str) else "".join(remote_tb))
    if frames and hasattr(clean, "add_note"):
        clean.add_note("student code traceback:\n" + frames)
    return cast(Exception, clean)


def _remote(method: Callable[..., Any], *args: object) -> Any:
    __tracebackhide__ = True
    try:
        return method(*args)
    except EOFError:
        raise
    except Exception as e:
        raise _clean(e) from None


# Larger plain-data replies are refused before decoding, so student code can't
# exhaust the grader's memory with a huge return value.
MAX_REPLY_CHARS = 16 * 1024 * 1024


def _check_reply(reply: object) -> tuple[str, str | int]:
    if (
        type(reply) is tuple
        and len(reply) == 2
        and reply[0] in ("plain", "ref")
        and type(reply[1]) is (str if reply[0] == "plain" else int)
    ):
        kind, value = cast(tuple[str, Any], reply)
        if kind == "plain" and len(value) > MAX_REPLY_CHARS:
            raise IsolationError(
                f"student code returned more than {MAX_REPLY_CHARS // (1024 * 1024)} MiB of data"
            )
        return kind, value
    raise IsolationError("malformed reply from the student process")


class Session:
    """One connection to the runner (one fresh student process)."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        config = dict(GRADER_CONFIG)
        # Leave the runner a moment to hit its own alarm first.
        config["sync_request_timeout"] = timeout + 2 if timeout > 0 else None
        self.conn = unix_connect(socket_path, service=rpyc.VoidService, config=config)
        self.root = self.conn.root
        self.root.begin(timeout)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def unpack(self, reply: object) -> Any:
        kind, value = _check_reply(reply)
        return decode(cast(str, value)) if kind == "plain" else Proxy(self, cast(int, value))

    def pack(self, obj: object) -> str:
        def on_other(value: object) -> dict[str, object]:
            if isinstance(value, Proxy):
                return {"$ref": value._pedagog_handle()}
            raise IsolationError(
                f"can't pass {type(value).__name__} to student code under process isolation; "
                "only plain data (numbers, strings, bytes, lists, tuples, dicts, sets) and "
                "student objects can cross (use `isolation: in-process` for callbacks)"
            )

        try:
            return encode(obj, on_other)
        except NotPlain as e:
            raise IsolationError(str(e)) from e


class _Current:
    """The session that student-module attributes resolve against right now."""

    session: Session | None = None


current = _Current()


def _active() -> Session:
    if current.session is None:
        raise IsolationError("student code is only reachable during collection and tests")
    return current.session


def _op(name: str, *args: object) -> Any:
    __tracebackhide__ = True
    session = _active()
    return session.unpack(_remote(session.root.op, name, session.pack(args)))


class Proxy:
    """A student object in the runner. Valid only within the test that made it."""

    __slots__ = ("_pedagog_session", "_pedagog_id")
    _pedagog_session: Session | None
    _pedagog_id: int | None

    def __init__(self, session: Session | None, handle: int | None) -> None:
        object.__setattr__(self, "_pedagog_session", session)
        object.__setattr__(self, "_pedagog_id", handle)

    def _pedagog_handle(self) -> int:
        if self._pedagog_session is not current.session or current.session is None or self._pedagog_id is None:
            raise IsolationError("this student object belongs to an earlier test")
        return self._pedagog_id

    def __getattr__(self, name: str) -> Any:
        __tracebackhide__ = True
        if name.startswith("_pedagog"):
            raise AttributeError(name)
        session = _active()
        return session.unpack(_remote(session.root.getattr, self._pedagog_handle(), name))

    def __setattr__(self, name: str, value: object) -> None:
        __tracebackhide__ = True
        session = _active()
        _remote(session.root.setattr, self._pedagog_handle(), name, session.pack(value))

    def __call__(self, *args: object, **kwargs: object) -> Any:
        __tracebackhide__ = True
        session = _active()
        return session.unpack(_remote(session.root.call, self._pedagog_handle(), session.pack((list(args), kwargs))))

    def __repr__(self) -> str:
        return cast(str, _op("repr", self))

    def __str__(self) -> str:
        return cast(str, _op("str", self))

    def __len__(self) -> int:
        return cast(int, _op("len", self))

    def __bool__(self) -> bool:
        return cast(bool, _op("bool", self))

    def __hash__(self) -> int:
        return cast(int, _op("hash", self))

    def __iter__(self) -> Any:
        return _op("iter", self)

    def __next__(self) -> Any:
        return _op("next", self)

    def __contains__(self, item: object) -> bool:
        return cast(bool, _op("contains", self, item))

    def __getitem__(self, key: object) -> Any:
        return _op("getitem", self, key)

    def __setitem__(self, key: object, value: object) -> None:
        _op("setitem", self, key, value)

    def __delitem__(self, key: object) -> None:
        _op("delitem", self, key)

    def __int__(self) -> int:
        return cast(int, _op("int", self))

    def __float__(self) -> float:
        return cast(float, _op("float", self))

    def __index__(self) -> int:
        return cast(int, _op("index", self))


_BinaryOp = Callable[[Proxy, object], Any]


def _binary(name: str) -> tuple[_BinaryOp, _BinaryOp]:
    def forward(self: Proxy, other: object) -> Any:
        __tracebackhide__ = True
        return _op(name, self, other)

    def reflected(self: Proxy, other: object) -> Any:
        __tracebackhide__ = True
        return _op(name, other, self)

    return forward, reflected


def _unary(name: str) -> Callable[[Proxy], Any]:
    def apply(self: Proxy) -> Any:
        return _op(name, self)

    return apply


for _name in ("eq", "ne", "lt", "le", "gt", "ge"):
    setattr(Proxy, f"__{_name}__", _binary(_name)[0])
for _name in ("add", "sub", "mul", "truediv", "floordiv", "mod", "pow", "matmul",
              "and", "or", "xor", "lshift", "rshift"):
    _fwd, _rev = _binary(_name)
    setattr(Proxy, f"__{_name}__", _fwd)
    setattr(Proxy, f"__r{_name}__", _rev)
for _name in ("neg", "pos", "abs", "invert"):
    setattr(Proxy, f"__{_name}__", _unary(_name))


class LateProxy(Proxy):
    """A student-module attribute, re-resolved in whichever test uses it.

    ``from linked_list import LinkedList`` runs once at collection, but each test
    talks to a fresh runner process, so the name must resolve per test.
    """

    __slots__ = ("_pedagog_path",)
    _pedagog_path: tuple[str, ...]

    def __init__(self, path: tuple[str, ...]) -> None:
        super().__init__(None, None)
        object.__setattr__(self, "_pedagog_path", path)

    def _pedagog_handle(self) -> int:
        session = _active()
        if self._pedagog_session is not session:
            module, *attrs = self._pedagog_path
            value = session.unpack(session.root.load(module))
            for attr in attrs:
                value = getattr(value, attr)
            if not isinstance(value, Proxy):
                raise IsolationError(f"{'.'.join(self._pedagog_path)} is plain data, not an object")
            object.__setattr__(self, "_pedagog_session", session)
            object.__setattr__(self, "_pedagog_id", value._pedagog_id)
        assert self._pedagog_id is not None
        return self._pedagog_id


_resolving = threading.local()


class StudentModule(types.ModuleType):
    """Stands in for a student module in the grader process."""

    def __getattr__(self, name: str) -> Any:
        # Never re-enter (e.g. from library code probing sys.modules mid-request).
        if name.startswith("__") or getattr(_resolving, "active", False):
            raise AttributeError(name)
        _resolving.active = True
        try:
            session = _active()
            module = session.unpack(session.root.load(self.__name__))
            if not isinstance(module, Proxy) or not session.root.hasattr(module._pedagog_handle(), name):
                raise AttributeError(f"student module {self.__name__!r} has no attribute {name!r}")
            value = getattr(module, name)
        finally:
            _resolving.active = False
        # Plain module-level constants are copied; objects resolve per test.
        return value if not isinstance(value, Proxy) else LateProxy((self.__name__, name))


class StudentFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Makes `import <student module>` in tests yield a StudentModule."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = frozenset(names)

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname.split(".")[0] not in self.names:
            return None
        # Submodules must really exist: `from pkg import missing` otherwise falls
        # back to importing `pkg.missing`, which we'd happily fabricate.
        if "." in fullname and (current.session is None or current.session.root.has_module(fullname) is not True):
            return None
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        module = StudentModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module: types.ModuleType) -> None:
        pass


def install(names: object) -> StudentFinder:
    if type(names) is not tuple or not all(type(n) is str for n in names):
        raise IsolationError("malformed module list from the student process")
    finder = StudentFinder(cast(tuple[str, ...], names))
    sys.meta_path.insert(0, finder)
    return finder
