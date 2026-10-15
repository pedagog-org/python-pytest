"""The root launcher behind ``/pedagog/bin/run-tests``.

Runs as root in a validation or grading pod and immediately splits into two
unprivileged processes:

- the student-code runner server, as ``runner`` (process isolation only);
- pytest over the hidden tests, as ``grader``.

Nothing student-controlled ever runs as root, neither child inherits the
launcher's environment (in particular the presigned results URL), and the
launcher itself uploads results.yaml once pytest has finished.
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .isolation.server import _become_subreaper

GRADER_USER = "grader"
RUNNER_USER = "runner"
RESULTS_URL_ENV = "PEDAGOG_RESULTS_URL"
SOCKET_WAIT_SECONDS = 15
# Per-user process cap for student code (helper processes yes, fork bombs no).
RUNNER_MAX_PROCS = 64
# Only these reach the children; everything else (notably the results URL) stays here.
PASSTHROUGH_ENV = ("PATH", "LANG", "LC_ALL", "TZ")
REAP_SECONDS = 5.0


class LaunchError(Exception):
    pass


def _clean_env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", **extra)
    return env


def _as(user: str) -> list[str]:
    return ["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", "--"]


def _chown_tree(path: Path, user: str) -> None:
    entry = pwd.getpwnam(user)
    for root, dirs, files in os.walk(path):
        os.chown(root, entry.pw_uid, entry.pw_gid)
        for name in dirs + files:
            os.chown(os.path.join(root, name), entry.pw_uid, entry.pw_gid, follow_symlinks=False)


def _make_readable(path: Path) -> None:
    """a+rX: in-process mode imports the copy as grader."""
    for root, dirs, files in os.walk(path):
        os.chmod(root, os.stat(root).st_mode | 0o555)
        for name in files:
            full = os.path.join(root, name)
            if not os.path.islink(full):
                os.chmod(full, os.stat(full).st_mode | 0o444)


def _home(work: Path, user: str) -> Path:
    home = work / f"{user}-home"
    home.mkdir()
    _chown_tree(home, user)
    return home


def _start_runner(work: Path, code: Path) -> tuple[subprocess.Popen[bytes], Path]:
    sock_dir = work / "sock"
    sock_dir.mkdir()
    _chown_tree(sock_dir, RUNNER_USER)
    sock = sock_dir / "runner.sock"
    cmd = _as(RUNNER_USER) + [
        sys.executable, "-m", "pedagog_pytest.isolation.server", "--socket", str(sock), "--code", str(code),
        "--sweep", "--max-procs", str(RUNNER_MAX_PROCS),
    ]
    # Runner processes orphaned when it's stopped come to us to be reaped.
    _become_subreaper()
    # Student output goes nowhere, so it can't flood the run's logs.
    proc = subprocess.Popen(
        cmd,
        env=_clean_env(_home(work, RUNNER_USER)),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + SOCKET_WAIT_SECONDS
    while not sock.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise LaunchError("the student-code runner failed to start")
        time.sleep(0.05)
    return proc, sock


def _stop(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    # Anything student code started that escaped the process group: kill every
    # remaining runner process (kill(-1) as runner spares only the caller).
    subprocess.run(
        _as(RUNNER_USER) + [sys.executable, "-c", "import os, signal\ntry:\n os.kill(-1, signal.SIGKILL)\nexcept ProcessLookupError:\n pass"],
        env=_clean_env(Path("/")),
        check=False,
    )
    _reap_orphans()


def _reap_orphans() -> None:
    """Collect the killed runner processes re-parented to us, so none linger as
    zombies (a container's PID 1 may never reap them, and they'd still count
    against the runner's process limit)."""
    deadline = time.monotonic() + REAP_SECONDS
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            time.sleep(0.01)


def _upload(path: Path, url: str) -> None:
    request = urllib.request.Request(url, data=path.read_bytes(), method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
    except OSError as e:  # URLError, HTTPError, timeouts
        raise LaunchError(f"results upload failed: {e}") from e
    if status >= 300:
        raise LaunchError(f"results upload failed: HTTP {status}")


def run(code: Path, tests: Path, results: Path, isolation: str, pytest_args: list[str]) -> Path:
    if os.geteuid() != 0:
        raise LaunchError("run-tests must start as root (it drops to grader/runner immediately)")
    if not tests.is_dir():
        raise LaunchError(f"no tests at {tests}")
    if not code.is_dir():
        raise LaunchError(f"no code to test at {code}")

    work = Path(tempfile.mkdtemp(prefix="pedagog-run-"))
    os.chmod(work, 0o755)
    runner = None
    try:
        # The originals may be grader-only (e.g. /pedagog/solution); the copy is
        # what the runner (or, in-process, the grader) imports.
        code_copy = work / "code"
        shutil.copytree(code, code_copy, symlinks=True)
        _chown_tree(code_copy, RUNNER_USER)
        _make_readable(code_copy)

        results.mkdir(parents=True, exist_ok=True)
        _chown_tree(results, GRADER_USER)
        (results / "results.yaml").unlink(missing_ok=True)

        env = _clean_env(_home(work, GRADER_USER), PEDAGOG_RESULTS_DIR=str(results), PEDAGOG_ISOLATION=isolation)
        if isolation == "process":
            runner, sock = _start_runner(work, code_copy)
            env["PEDAGOG_RUNNER_SOCKET"] = str(sock)
        else:
            env["PYTHONPATH"] = str(code_copy)

        cmd = _as(GRADER_USER) + [
            sys.executable, "-m", "pytest", str(tests), "--rootdir", str(tests),
            "-p", "no:cacheprovider", *pytest_args,
        ]
        # Test failures are results, not launcher errors; only a missing
        # results.yaml is.
        subprocess.run(cmd, env=env, cwd=work)
    finally:
        if runner is not None:
            _stop(runner)
        shutil.rmtree(work, ignore_errors=True)

    produced = results / "results.yaml"
    if not produced.exists():
        raise LaunchError("pytest did not produce results.yaml")
    return produced


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run pedagog hidden tests (as root; drops privileges)")
    parser.add_argument("--code", type=Path, required=True, help="code under test (e.g. /pedagog/solution)")
    parser.add_argument("--tests", type=Path, default=Path("/pedagog/tests"))
    parser.add_argument("--results", type=Path, default=Path("/pedagog/results"))
    parser.add_argument("--isolation", choices=("process", "in-process"),
                        default=os.environ.get("PEDAGOG_ISOLATION", "process"))
    parser.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    args = parser.parse_args(argv)
    try:
        produced = run(args.code, args.tests, args.results, args.isolation, args.pytest_args)
        url = os.environ.get(RESULTS_URL_ENV)
        if url:
            _upload(produced, url)
        print(f"results: {produced}")
    except LaunchError as e:
        print(f"run-tests: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
