#!/bin/bash
# Wrapper script to ensure HelmPack runs with the correct virtual environment

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if virtual environment exists
VENV_DIR="$SCRIPT_DIR/helmpack-env"
if [ ! -d "$VENV_DIR" ]; then
    echo "❌ Virtual environment not found at $VENV_DIR"
    echo "Please run: python3 -m venv helmpack-env"
    exit 1
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Check if required packages are installed
if ! python -c "import click, yaml, requests" 2>/dev/null; then
    echo "❌ Required packages not installed in virtual environment"
    echo "Please run: source helmpack-env/bin/activate && pip install -r requirements-minimal.txt"
    exit 1
fi

# Run HelmPack with the virtual environment's Python
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/helmpack.py" "$@"