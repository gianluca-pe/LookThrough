"""Shell startup uses the local interpreter after a project folder is moved."""

import json
from pathlib import Path
import shutil
import subprocess
import sys


def test_launcher_ignores_stale_console_script_after_move(tmp_path):
    project = tmp_path / "moved project"
    bin_dir = project / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    stale_script = bin_dir / "waitress-serve"
    stale_script.write_text("#!/nonexistent-old-project/.venv/bin/python\n")
    stale_script.chmod(0o755)
    launcher = project / "run.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "run.sh", launcher)
    # A disposable module records invocation without opening a socket or database.
    (project / "waitress.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))\n"
    )

    result = subprocess.run(
        ["/bin/sh", str(launcher)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    invocation = json.loads(result.stdout)
    assert Path(invocation["cwd"]) == project.resolve()
    assert invocation["args"] == [
        "--call", "--host=127.0.0.1", "--port=5001", "app:create_app",
    ]
