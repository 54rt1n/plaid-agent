import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("entry", ["console", "module"])
def test_cli_ignores_foreign_python_packages_before_importing_application(tmp_path, entry):
    foreign = tmp_path / "foreign-python"
    pydantic = foreign / "pydantic"
    pydantic.mkdir(parents=True)
    (pydantic / "__init__.py").write_text('raise RuntimeError("Foreign Python packages loaded")\n')
    env = {**os.environ, "PYTHONPATH": str(foreign)}
    executable = Path(sys.executable).parent / "plaid-agent"
    command = [str(executable)] if entry == "console" else [sys.executable, "-m", "plaid_cli"]
    data = tmp_path / "data"
    initialized = subprocess.run(
        command + ["--data-dir", str(data), "init"],
        env=env,
        cwd=foreign,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert initialized.returncode == 0, initialized.stderr
    status = subprocess.run(
        command + ["--data-dir", str(data), "--json", "status"],
        env=env,
        cwd=foreign,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["environment"] == "sandbox"
