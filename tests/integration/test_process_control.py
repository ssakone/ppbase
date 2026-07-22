from __future__ import annotations

import json
import sys
from pathlib import Path

from ppbase.services import process_control


def test_get_restart_command_uses_env_when_present(monkeypatch) -> None:
    monkeypatch.setenv(
        "PBBASE_RESTART_CMD",
        json.dumps(["/usr/bin/python3", "/tmp/app.py", "--port", "8090"]),
    )
    assert process_control.get_restart_command() == [
        "/usr/bin/python3",
        "/tmp/app.py",
        "--port",
        "8090",
    ]


def test_get_restart_command_infers_script_launch(monkeypatch) -> None:
    monkeypatch.delenv("PBBASE_RESTART_CMD", raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", ["main.py", "--dev", "--port", "8090"])
    monkeypatch.setattr(sys.modules["__main__"], "__file__", "main.py", raising=False)

    assert process_control.get_restart_command() == [
        "/usr/bin/python3",
        str(Path("main.py").resolve()),
        "--dev",
        "--port",
        "8090",
    ]


def test_get_restart_command_falls_back_to_inference_on_invalid_env(monkeypatch) -> None:
    monkeypatch.setenv("PBBASE_RESTART_CMD", "not-json")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", ["run_app.py"])
    monkeypatch.setattr(sys.modules["__main__"], "__file__", "run_app.py", raising=False)

    assert process_control.get_restart_command() == [
        "/usr/bin/python3",
        str(Path("run_app.py").resolve()),
    ]
