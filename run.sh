#!/bin/sh
# Start LookThrough locally: repo-local venv, localhost only with Waitress.
# Selection and any confirmed upgrade happen in the local browser flow.
set -e
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -e "$PY" ] || ! "$PY" --version >/dev/null 2>&1; then
    if [ -d .venv ]; then
        cat >&2 <<'EOF'
The virtual environment in .venv is broken: its Python interpreter is gone.
This usually happens after a system Python upgrade or after moving the project.
Recreate it from this folder:

    rm -rf .venv
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt -c requirements-verified.txt

Then run ./run.sh again.
EOF
    else
        cat >&2 <<'EOF'
No virtual environment found. Set it up from this folder (see README.md,
"Install and run"):

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt -c requirements-verified.txt

Then run ./run.sh again.
EOF
    fi
    exit 1
fi

# Use the local interpreter; generated console scripts can retain a moved path.
exec .venv/bin/python -m waitress --call --host=127.0.0.1 --port=5001 app:create_app
