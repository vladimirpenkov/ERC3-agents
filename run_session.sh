#!/bin/bash
# Run full session with all tasks
# Usage: ./run_session.sh
# Note: Activate your Python environment before running

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
mkdir -p logs
"$SCRIPT_DIR/del_compiled.sh"
python -u main.py "$@" 2>&1 | tee logs/sess_console.txt
