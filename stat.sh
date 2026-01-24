#!/bin/bash
# Usage: ./stat.sh ssn-42ZDwQjdgfVcQMVvNXpehh

if [ -z "$1" ]; then
    echo "Usage: ./stat.sh <session_id>"
    echo "Example: ./stat.sh ssn-42ZDwQjdgfVcQMVvNXpehh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SESSION_DIR=$(find logs/sessions -maxdepth 1 -type d -name "*$1*" 2>/dev/null | head -1)

if [ -z "$SESSION_DIR" ]; then
    echo "Session not found: $1"
    exit 1
fi

python scripts/llm_time_stats.py "$SESSION_DIR"
