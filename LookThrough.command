#!/bin/sh
# Finder-friendly launcher for the local LookThrough server.

set -u

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HEALTH_URL="http://127.0.0.1:5001/health"
CHOOSER_URL="http://127.0.0.1:5001/"

cd "$APP_DIR" || exit 1

ready() {
    # Health checks must stay local regardless of proxy variables or ~/.curlrc.
    # Keep the actual error for timeout diagnostics instead of discarding it.
    PROBE_ERROR=$(/usr/bin/curl -q --noproxy '*' --silent --show-error \
        --fail --max-time 1 "$HEALTH_URL" 2>&1 >/dev/null)
}

# A second launch should open the application, not start a competing server.
if ready; then
    /usr/bin/open "$CHOOSER_URL"
    exit $?
fi

echo "Starting LookThrough. The first launch can take longer; waiting up to 60 seconds."
./run.sh &
SERVER_PID=$!

stop_server() {
    if kill -0 "$SERVER_PID" >/dev/null 2>&1; then
        kill "$SERVER_PID" >/dev/null 2>&1
        wait "$SERVER_PID" 2>/dev/null
    fi
}

trap 'stop_server; exit 130' HUP INT TERM

# Use elapsed time, not a short count of polling attempts: a cold Python startup
# can take longer than the old roughly ten-second allowance.
STARTED=$(/bin/date +%s)
LAST_NOTICE=0
while :; do
    if ready; then
        break
    fi

    if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
        wait "$SERVER_PID"
        STATUS=$?
        echo "LookThrough stopped during startup (status $STATUS)." >&2
        echo "The startup output is above. To retry directly, run ./run.sh from the LookThrough folder." >&2
        [ "$STATUS" -ne 0 ] || STATUS=1
        exit "$STATUS"
    fi

    ELAPSED=$(($(/bin/date +%s) - STARTED))
    if [ "$ELAPSED" -ge 60 ]; then
        echo "LookThrough did not become ready within 60 seconds; stopping this startup attempt." >&2
        printf 'Local readiness check: %s\n' "$PROBE_ERROR" >&2
        echo "To see startup without this time limit, run ./run.sh from the LookThrough folder." >&2
        stop_server
        exit 1
    fi

    if [ "$((ELAPSED - LAST_NOTICE))" -ge 10 ]; then
        echo "Still starting LookThrough ($ELAPSED seconds)..."
        LAST_NOTICE=$ELAPSED
    fi
    /bin/sleep 0.2
done

if /usr/bin/open "$CHOOSER_URL"; then
    echo "LookThrough is open in your browser. Keep this window open while using it."
else
    echo "LookThrough is running. Open $CHOOSER_URL in your browser."
fi
echo "Press Control-C here when you want to stop LookThrough."
wait "$SERVER_PID"
STATUS=$?
if [ "$STATUS" -ne 0 ]; then
    echo "LookThrough stopped (status $STATUS). Review the server output above." >&2
fi
exit "$STATUS"
