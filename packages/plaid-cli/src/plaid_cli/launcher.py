"""Enter the CLI with its own interpreter's packages before importing the app."""

import os
import sys


def main():
    if not sys.flags.isolated:
        # Hermes may export a PYTHONPATH for a different Python ABI to child tools.
        os.execv(sys.executable, [sys.executable, "-I", "-m", "plaid_cli", *sys.argv[1:]])
    from .main import main as run

    return run()
