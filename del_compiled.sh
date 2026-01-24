#!/bin/bash
# Delete Python compilation artifacts and binary files
# Excludes venv directory to preserve installed packages

find . -path "./venv" -prune -o -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find . -path "./venv" -prune -o -type f -name "*.pyc" -delete 2>/dev/null
find . -path "./venv" -prune -o -type f -name "*.pyo" -delete 2>/dev/null
find . -path "./venv" -prune -o -type f -name "*.pyd" -delete 2>/dev/null
find . -path "./venv" -prune -o -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null
find . -path "./venv" -prune -o -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null
find . -path "./venv" -prune -o -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null

echo "Compilation artifacts deleted"
