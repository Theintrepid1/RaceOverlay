#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Starts the Race Overlay sync GUI (or Race Analysis with --analysis). Used by
# the desktop launchers; also fine to run directly. Any other arguments (e.g. a
# video path) are passed through.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/raceoverlay/launch.log"
mkdir -p "$(dirname "$LOG")"

popup() {  # title, message — best available dialog, since there's no terminal
    if command -v kdialog >/dev/null; then kdialog --title "$1" --error "$2"
    elif command -v zenity >/dev/null; then zenity --error --title="$1" --text="$2"
    elif command -v notify-send >/dev/null; then notify-send -u critical "$1" "$2"
    else echo "$1: $2" >&2; fi
}

PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
    popup "Race Overlay" "No Python environment found in:
$DIR

Set it up once in a terminal:
cd '$DIR'
python -m venv .venv
.venv/bin/pip install -r requirements.txt"
    exit 1
fi

MODULE=raceoverlay.syncgui
if [ "${1:-}" = "--analysis" ]; then MODULE=raceanalysis; shift; fi
cd "$DIR"
PYTHONPATH="$DIR/src${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m "$MODULE" "$@" 2>"$LOG"
rc=$?
# Exit 1 = the app already showed its own error dialog (or the user cancelled
# a file picker). Anything else unexpected: show the end of the log.
if [ $rc -ne 0 ] && [ $rc -ne 1 ]; then
    popup "Race Overlay crashed" "Exit code $rc. Last lines of $LOG:

$(tail -n 15 "$LOG")"
fi
exit $rc
