"""Test configuration module."""

import pytest
from src.config import Settings


def test_settings_defaults():
    """Test default settings values."""
    # This will use .env.sample or raise error if required fields are missing
    # For testing, we'll create a minimal settings object
    settings = Settings(
        azure_openai_endpoint="https://test.openai.azure.com/",
        azure_openai_api_key="test-key",
        azure_openai_deployment="gpt-5",
        mcp_customer_query_url="http://localhost:5001",
        mcp_destination_recommendation_url="http://localhost:5002",
        mcp_itinerary_planning_url="http://localhost:5003",
        mcp_echo_ping_url="http://localhost:5004",
    )

    assert settings.port == 4000
    assert settings.log_level == "INFO"
    assert settings.otel_service_name == "api-python"
    assert settings.azure_openai_api_version == "2024-02-15-preview"


def test_settings_custom_port():
    """Test custom port setting."""
    settings = Settings(
        azure_openai_endpoint="https://test.openai.azure.com/",
        azure_openai_api_key="test-key",
        azure_openai_deployment="gpt-5",
        mcp_customer_query_url="http://localhost:5001",
        mcp_destination_recommendation_url="http://localhost:5002",
        mcp_itinerary_planning_url="http://localhost:5003",
        mcp_echo_ping_url="http://localhost:5004",
        port=5000,
    )

    assert settings.port == 5000


@pytest.mark.parametrize(
    ("server_id", "suffix"),
    [
        ("echo-ping", "/mcp"),
        ("customer-query", "/mcp"),
        ("itinerary-planning", "/mcp/"),
        ("destination-recommendation", "/mcp"),
    ],
)
def test_mcp_server_paths(monkeypatch, server_id, suffix):
    from src.orchestrator.tools.tool_config import get_mcp_tools_config

    for name in (
        "MCP_ECHO_PING_URL",
        "MCP_CUSTOMER_QUERY_URL",
        "MCP_ITINERARY_PLANNING_URL",
        "MCP_DESTINATION_RECOMMENDATION_URL",
    ):
        monkeypatch.setenv(name, "http://localhost:5003")

    assert get_mcp_tools_config()[server_id]["config"]["url"] == f"http://localhost:5003{suffix}"
