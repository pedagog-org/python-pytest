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
