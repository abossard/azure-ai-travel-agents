"""Test application logging initialization."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("azure_monitor", [False, True], ids=["local", "azure-monitor"])
def test_console_and_telemetry_logging(azure_monitor):
    env = os.environ.copy()
    env["LOG_LEVEL"] = "INFO"
    if azure_monitor:
        env["APPLICATIONINSIGHTS_CONNECTION_STRING"] = "test"
    else:
        env.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)

    script = """
import io
import logging
import sys
from unittest.mock import patch

telemetry = io.StringIO()

def configure_telemetry():
    logging.getLogger().addHandler(logging.StreamHandler(telemetry))

with patch("agent_framework.observability.configure_otel_providers", configure_telemetry), \\
     patch("azure.monitor.opentelemetry.configure_azure_monitor", configure_telemetry):
    import src.main

logging.getLogger("test").error("logging-probe")
assert any(
    isinstance(handler, logging.StreamHandler) and handler.stream is sys.stderr
    for handler in logging.getLogger().handlers
)
assert "logging-probe" in telemetry.getvalue()
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)
    assert "logging-probe" in result.stderr
