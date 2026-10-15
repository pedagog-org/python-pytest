"""The root launcher with real users. Needs root, `setpriv`, and the `grader` and
`runner` users (pedagog images have all three); skipped elsewhere. CI runs it
in a container that creates the users."""

import http.server
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest


def _users_exist() -> bool:
    try:
        pwd.getpwnam("grader")
        pwd.getpwnam("runner")
    except KeyError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or not _users_exist() or shutil.which("setpriv") is None,
    reason="needs root, setpriv, and the grader/runner users",
)


def grader_only(path: Path) -> None:
    entry = pwd.getpwnam("grader")
    for root, dirs, files in os.walk(path):
        os.chown(root, entry.pw_uid, entry.pw_gid)
        for name in files:
            os.chown(os.path.join(root, name), entry.pw_uid, entry.pw_gid)
    os.chmod(path, 0o700)


@pytest.fixture
def assignment(tmp_path):
    """Lays out grader-only tests and code under test like an assignment image."""

    def make(tests: str, code: dict[str, str]) -> tuple[Path, Path, Path]:
        # pytest's temp root is 0700; images keep /pedagog traversable.
        for path in (tmp_path, *tmp_path.parents):
            if path == Path(tempfile.gettempdir()):
                break
            os.chmod(path, 0o755)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_suite.py").write_text(textwrap.dedent(tests))
        code_dir = tmp_path / "solution"
        code_dir.mkdir()
        for name, src in code.items():
            (code_dir / f"{name}.py").write_text(textwrap.dedent(src))
        grader_only(tests_dir)
        grader_only(code_dir)
        return tests_dir, code_dir, tmp_path / "results"

    return make


def launch(tests: Path, code: Path, results: Path, *args: str, env: dict | None = None):
    proc = subprocess.run(
        [sys.executable, "-m", "pedagog_pytest.launcher", "--code", str(code), "--tests", str(tests),
         "--results", str(results), *args],
        capture_output=True, text=True, env={**os.environ, **(env or {})},
    )
    return proc


def outcomes(results: Path) -> dict:
    doc = json.loads((results / "results.yaml").read_text())
    return {t["id"].split("::")[-1]: t for t in doc["tests"]}


SNOOP = """
    import os
    import pwd

    def whoami():
        return pwd.getpwuid(os.getuid()).pw_name

    def read(path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return "denied"

    def env(name):
        return os.environ.get(name, "absent")
"""


def test_process_isolation_separates_users(assignment):
    tests, code, results = assignment(
        """
        import os, pwd
        from snoop import whoami, read, env

        TESTS_FILE = os.path.abspath(__file__)

        def test_student_code_runs_as_runner():
            assert whoami() == "runner"

        def test_student_code_cannot_read_the_tests():
            assert read(TESTS_FILE) == "denied"

        def test_student_code_never_sees_the_results_url():
            assert env("PEDAGOG_RESULTS_URL") == "absent"

        def test_harness_runs_as_grader_without_the_url():
            assert pwd.getpwuid(os.getuid()).pw_name == "grader"
            assert "PEDAGOG_RESULTS_URL" not in os.environ
        """,
        {"snoop": SNOOP},
    )
    proc = launch(tests, code, results, env={"PEDAGOG_RESULTS_URL": "http://127.0.0.1:9/never"})
    # The upload fails (nothing listens): a clean launcher error, after the run happened.
    assert proc.returncode == 2
    assert "results upload failed" in proc.stderr
    got = outcomes(results)
    failed = {k: v["output"] for k, v in got.items() if v["status"] != "pass"}
    assert len(got) == 4 and not failed, failed


def test_results_are_uploaded_to_the_presigned_url(assignment):
    tests, code, results = assignment(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        {"calc": "def add(a, b):\n    return a + b\n"},
    )
    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    proc = launch(tests, code, results, env={"PEDAGOG_RESULTS_URL": f"http://127.0.0.1:{server.server_port}/r"})
    server.server_close()
    assert proc.returncode == 0, proc.stderr
    assert json.loads(received["body"])["tests"][0]["status"] == "pass"


def test_in_process_mode_imports_student_code_directly(assignment):
    tests, code, results = assignment(
        """
        import os, pwd
        from snoop import whoami

        def test_same_process_as_grader():
            assert whoami() == "grader"
        """,
        {"snoop": SNOOP},
    )
    proc = launch(tests, code, results, "--isolation", "in-process")
    assert proc.returncode == 0, proc.stderr
    assert outcomes(results)["test_same_process_as_grader"]["status"] == "pass"


def test_refuses_to_run_without_root(assignment):
    tests, code, results = assignment("def test_x():\n    pass\n", {})
    proc = subprocess.run(
        ["setpriv", "--reuid=runner", "--regid=runner", "--init-groups", "--",
         sys.executable, "-m", "pedagog_pytest.launcher", "--code", str(code), "--tests", str(tests),
         "--results", str(results)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "must start as root" in proc.stderr


ATTACKER = """
    import os
    import signal
    import sys
    import time

    def _try(action):
        try:
            action()
            return "succeeded"
        except OSError:
            return "denied"

    def forge_results(results_dir):
        def write():
            with open(os.path.join(results_dir, "results.yaml"), "w") as f:
                f.write('{"tests": [], "summary": {"score": 100, "total": 100}}')
        return _try(write)

    def tamper_with_tests(tests_file):
        def write():
            with open(tests_file, "a") as f:
                f.write("\\n# owned")
        return _try(write)

    def _pids_of(uid):
        pids = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    if os.stat(f"/proc/{entry}").st_uid == uid:
                        pids.append(int(entry))
                except FileNotFoundError:
                    pass
        return pids

    def kill_the_grader(grader_uid):
        targets = _pids_of(grader_uid)
        if not targets:
            return "no grader process found"
        return _try(lambda: [os.kill(pid, signal.SIGKILL) for pid in targets])

    def read_grader_proc(grader_uid):
        pid = _pids_of(grader_uid)[0]
        leaks = []
        for name in ("environ", "mem", "cmdline_fd"):
            path = f"/proc/{pid}/{name}" if name != "cmdline_fd" else f"/proc/{pid}/fd/0"
            try:
                with open(path, "rb") as f:
                    f.read(16)
                leaks.append(name)
            except OSError:
                pass
        return leaks or "denied"

    def leave_a_background_process():
        if os.fork() == 0:
            os.setsid()
            if os.fork() == 0:
                time.sleep(120)
            os._exit(0)
        return "started"

    def other_runner_processes():
        mine = {os.getpid(), os.getppid()}
        return sorted(p for p in _pids_of(os.getuid()) if p not in mine)

    def fork_bomb():
        children = 0
        try:
            while children < 10_000:
                if os.fork() == 0:
                    time.sleep(120)
                    os._exit(0)
                children += 1
        except OSError:
            pass
        return children

    def flood_output():
        chunk = "x" * 1_000_000
        for _ in range(50):
            sys.stdout.write(chunk)
            sys.stderr.write(chunk)
        return "flooded"
"""


def test_student_code_cannot_cheat_or_interfere(assignment):
    grader_uid = pwd.getpwnam("grader").pw_uid
    tests, code, results = assignment(
        f"""
        import os
        from attacker import (
            forge_results, tamper_with_tests, kill_the_grader, read_grader_proc,
            leave_a_background_process, other_runner_processes, fork_bomb, flood_output,
        )

        RESULTS = os.environ["PEDAGOG_RESULTS_DIR"]
        TESTS_FILE = os.path.abspath(__file__)

        def test_1_cannot_forge_results():
            assert forge_results(RESULTS) == "denied"

        def test_2_cannot_tamper_with_tests():
            assert tamper_with_tests(TESTS_FILE) == "denied"

        def test_3_cannot_kill_the_grader():
            assert kill_the_grader({grader_uid}) == "denied"

        def test_4_cannot_read_grader_memory_or_environment():
            assert read_grader_proc({grader_uid}) == "denied"

        def test_5_starts_a_background_process():
            assert leave_a_background_process() == "started"

        def test_6_background_process_did_not_survive():
            assert other_runner_processes() == []

        def test_7_fork_bomb_is_capped():
            assert fork_bomb() < 64

        def test_8_tests_still_run_after_the_fork_bomb():
            assert other_runner_processes() == []

        def test_9_output_flood_goes_nowhere():
            assert flood_output() == "flooded"
        """,
        {"attacker": ATTACKER},
    )
    proc = launch(tests, code, results)
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = outcomes(results)
    failed = {k: v["output"][-1500:] for k, v in got.items() if v["status"] != "pass"}
    assert len(got) == 9 and not failed, failed
    # The 100 MB flood never reached the launcher's (and so the pod's) logs.
    assert len(proc.stdout) + len(proc.stderr) < 200_000
    # Nothing of the runner's outlives the launcher.
    leftover = subprocess.run(["sh", "-c", "ps -u runner -o pid= | wc -l"], capture_output=True, text=True)
    assert leftover.stdout.strip() == "0"
