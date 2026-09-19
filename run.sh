#!/bin/sh
# Start LookThrough locally: repo-local venv, localhost only with Waitress.
# Selection and any confirmed upgrade happen in the local browser flow.
set -e
cd "$(dirname "$0")"
# Use the local interpreter; generated console scripts can retain a moved path.
exec .venv/bin/python -m waitress --call --host=127.0.0.1 --port=5001 app:create_app
