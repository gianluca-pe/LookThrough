#!/bin/sh
# Finder-friendly launcher for the local LookThrough server.

set -u

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HEALTH_URL="http://127.0.0.1:5001/health"
CHOOSER_URL="http://127.0.0.1:5001/"

cd "$APP_DIR" || exit 1

# A second launch should open the application, not start a competing server.
if /usr/bin/curl --silent --fail --max-time 1 "$HEALTH_URL" >/dev/null 2>&1; then
    /usr/bin/open "$CHOOSER_URL"
    exit 0
fi

./run.sh &
SERVER_PID=$!

stop_server() {
    if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
        kill "$SERVER_PID" >/dev/null 2>&1
        wait "$SERVER_PID" 2>/dev/null
    fi
}

trap 'stop_server; exit 130' HUP INT TERM

# Wait for server startup before opening the chooser.
ATTEMPT=0
while [ "$ATTEMPT" -lt 100 ]; do
    if /usr/bin/curl --silent --fail --max-time 1 "$HEALTH_URL" >/dev/null 2>&1; then
        /usr/bin/open "$CHOOSER_URL"
        break
    fi

    if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
        wait "$SERVER_PID"
        exit $?
    fi

    ATTEMPT=$((ATTEMPT + 1))
    /bin/sleep 0.1
done

if [ "$ATTEMPT" -ge 100 ]; then
    echo "LookThrough did not become ready. Review the startup messages above."
    stop_server
    exit 1
fi

echo "LookThrough is open in your browser. Keep this window open while using it."
echo "Press Control-C here when you want to stop LookThrough."
wait "$SERVER_PID"
