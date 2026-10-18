"""Exercise the Finder shell launcher against real Waitress and disposable data.

Run: .venv/bin/python tests/launcher_e2e.py [--case CASE]
Artifacts: temporary directory printed at startup, including assertion JSON/logs.
Only macOS's browser-open command is replaced with a recording executable; the
port is allocated per fixture. No user's database or browser is opened.

Failure cases defined before the launcher change:
- A slow Python preflight exceeds the old ten-second readiness allowance.
- User curl configuration/proxy settings prevent a direct loopback health check.
- A second launch starts another server instead of reusing the healthy process.
- Early server failure loses its exit status or provides no actionable explanation.
- Readiness timeout leaves the server running or gives no diagnostic next step.
- Interrupt/termination leaves a server or falsely reports a successful launch.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


SOURCE = Path(__file__).resolve().parents[1]
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health(port):
    try:
        with HTTP.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            return response.read() == b"ok\n"
    except OSError:
        return False


def check(case, artifacts):
    project = artifacts / case / "project with spaces"
    project.mkdir(parents=True)
    for name in ("app", "migrations"):
        shutil.copytree(SOURCE / name, project / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    opened = project.parent / "opened.txt"
    opener = project.parent / "open-browser"
    opener.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {shlex.quote(str(opened))}\n")
    opener.chmod(0o755)
    for name in ("run.sh", "LookThrough.command"):
        content = (SOURCE / name).read_text().replace("5001", str(port))
        content = content.replace("/usr/bin/open", shlex.quote(str(opener)))
        (project / name).write_text(content)
        (project / name).chmod(0o755)
    bin_dir = project / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    delay = "sleep 12\n" if case == "delayed" else ""
    (bin_dir / "python").write_text(
        f"#!/bin/sh\n{delay}exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    (bin_dir / "python").chmod(0o755)
    env = dict(os.environ)
    if case == "curl-config":
        curl_home = project.parent / "curl-home"
        curl_home.mkdir()
        (curl_home / ".curlrc").write_text("proxy = http://127.0.0.1:1\nnoproxy = \"\"\n")
        env.update(CURL_HOME=str(curl_home), http_proxy="http://127.0.0.1:1",
                   ALL_PROXY="http://127.0.0.1:1", NO_PROXY="", no_proxy="")
    if case == "failure":
        (project / "run.sh").write_text("#!/bin/sh\necho 'Synthetic startup failure' >&2\nexit 23\n")
    if case == "timeout":
        (project / "run.sh").write_text("#!/bin/sh\nexec sleep 120\n")
    log_path = project.parent / "launcher.log"
    start = time.monotonic()
    with log_path.open("w") as log:
        process = subprocess.Popen(["/bin/sh", str(project / "LookThrough.command")],
                                   cwd=artifacts, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            if case in {"failure", "timeout"}:
                status = process.wait(timeout=75)
                output = log_path.read_text()
                assert status == (23 if case == "failure" else 1), output
                assert not opened.exists(), "Opened browser after failed startup"
                assert "./run.sh" in output, "No diagnostic next step"
                if case == "failure":
                    assert "Synthetic startup failure" in output
                    assert "status 23" in output
                else:
                    assert "60 seconds" in output
                    assert "curl:" in output, "Missing readiness failure detail"
            else:
                deadline = start + 50
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()
                    if opened.exists():
                        break
                    time.sleep(0.1)
                assert opened.exists(), "Browser was not opened"
                assert health(port), log_path.read_text()
                assert opened.read_text().splitlines() == [f"http://127.0.0.1:{port}/"]
                with HTTP.open(f"http://127.0.0.1:{port}/", timeout=3) as response:
                    assert response.status == 200
                second = subprocess.run(["/bin/sh", str(project / "LookThrough.command")],
                                        env=env, capture_output=True, text=True, timeout=5)
                assert second.returncode == 0, second.stdout + second.stderr
                assert len(opened.read_text().splitlines()) == 2
                assert process.poll() is None, "Second launch stopped original server"
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=5) == 130
                assert not health(port), "Server survived launcher termination"
            # All direct children must have been reaped before the launcher exits.
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("Launcher left a process group behind")
            assert not list(project.rglob("*.sqlite3")), "Health/empty chooser created a portfolio"
            return {"case": case, "passed": True,
                    "seconds": round(time.monotonic() - start, 2)}
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cases = ("delayed", "curl-config", "failure", "timeout")
    parser.add_argument("--case", choices=cases)
    args = parser.parse_args()
    artifacts = Path(tempfile.mkdtemp(prefix="lt-launcher-e2e-"))
    print(f"Artifacts: {artifacts}", flush=True)
    results = []
    for case in ([args.case] if args.case else cases):
        try:
            result = check(case, artifacts)
        except Exception as error:
            result = {"case": case, "passed": False, "error": str(error)}
        results.append(result)
        print(json.dumps(result), flush=True)
        (artifacts / "assertions.json").write_text(json.dumps(results, indent=2) + "\n")
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
