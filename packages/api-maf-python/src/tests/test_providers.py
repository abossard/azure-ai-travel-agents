"""Test LLM provider implementations."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.orchestrator.providers import get_llm_client
from src.orchestrator.providers.azure_openai import AzureOpenAIProvider
from src.orchestrator.providers.github_models import GitHubModelsProvider
from src.orchestrator.providers.docker_models import DockerModelsProvider
from src.orchestrator.providers.ollama_models import OllamaModelsProvider
from src.config import settings


@pytest.mark.asyncio
async def test_azure_openai_provider_local_docker():
    """Test Azure OpenAI provider in local Docker mode."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.azure_openai_endpoint = "https://test.openai.azure.com/"
        mock_settings.azure_openai_deployment = "gpt-5"
        mock_settings.azure_openai_api_key = "test-key"
        mock_settings.azure_openai_api_version = "2024-02-15-preview"
        mock_settings.is_local_docker_env = True

        with patch("src.orchestrator.providers.azure_openai.AsyncAzureOpenAI") as mock_client:
            provider = AzureOpenAIProvider()
            await provider.get_client()

            mock_client.assert_called_once_with(
                api_key="test-key",
                api_version="2024-02-15-preview",
                azure_endpoint="https://test.openai.azure.com/",
            )


@pytest.mark.asyncio
async def test_github_models_provider():
    """Test GitHub Models provider."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.github_token = "test-token"
        mock_settings.github_model = "openai/gpt-5"

        with patch("src.orchestrator.providers.github_models.AsyncOpenAI") as mock_client:
            provider = GitHubModelsProvider()
            await provider.get_client()

            mock_client.assert_called_once_with(
                base_url="https://models.inference.ai.azure.com",
                api_key="test-token",
            )


@pytest.mark.asyncio
async def test_docker_models_provider():
    """Test Docker Models provider."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.docker_model_endpoint = "http://localhost:12434/v1"
        mock_settings.docker_model = "ai/phi4:14B-Q4_0"

        with patch("src.orchestrator.providers.docker_models.AsyncOpenAI") as mock_client:
            provider = DockerModelsProvider()
            await provider.get_client()

            mock_client.assert_called_once_with(
                base_url="http://localhost:12434/v1",
                api_key="DOCKER_API_KEY",
            )


@pytest.mark.asyncio
async def test_ollama_models_provider():
    """Test Ollama Models provider."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.ollama_model_endpoint = "http://localhost:11434/v1"
        mock_settings.ollama_model = "llama3.1"

        with patch("src.orchestrator.providers.ollama_models.AsyncOpenAI") as mock_client:
            provider = OllamaModelsProvider()
            await provider.get_client()

            mock_client.assert_called_once_with(
                base_url="http://localhost:11434/v1",
                api_key="OLLAMA_API_KEY",
            )


@pytest.mark.asyncio
async def test_get_llm_client_azure_openai():
    """Test get_llm_client with Azure OpenAI provider."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.llm_provider = "azure-openai"
        mock_settings.azure_openai_endpoint = "https://test.openai.azure.com/"
        mock_settings.azure_openai_deployment = "gpt-5"
        mock_settings.azure_openai_api_key = "test-key"
        mock_settings.is_local_docker_env = True

        with patch("src.orchestrator.providers.azure_openai.AsyncAzureOpenAI"):
            client = await get_llm_client()
            assert client is not None


@pytest.mark.asyncio
async def test_get_llm_client_invalid_provider():
    """Test get_llm_client with invalid provider."""
    with patch("src.config.settings") as mock_settings:
        mock_settings.llm_provider = "invalid-provider"

        with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
            await get_llm_client()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_factory", [False, True], ids=["explicit-stack", "default-factory"])
@pytest.mark.parametrize("api_key", ["test-api-key", None], ids=["api-key", "managed-identity"])
async def test_azure_openai_authentication_and_lifecycle(monkeypatch, use_factory, api_key):
    from contextlib import AsyncExitStack

    import httpx
    from azure.core.credentials import AccessTokenInfo
    from openai import AsyncAzureOpenAI

    from src.orchestrator.providers import close_llm_clients
    from src.orchestrator.providers import azure_openai

    monkeypatch.setattr(settings, "llm_provider", "azure-openai")
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://example.openai.azure.com/")
    monkeypatch.setattr(settings, "azure_openai_deployment_name", "test-deployment")
    monkeypatch.setattr(settings, "azure_openai_api_version", "2024-02-15-preview")
    monkeypatch.setattr(settings, "azure_openai_api_key", api_key)
    monkeypatch.setattr(settings, "azure_client_id", "test-client")

    credential = MagicMock(spec=azure_openai.ManagedIdentityCredential)
    credential.get_token_info = AsyncMock(
        side_effect=[AccessTokenInfo("first-token", 0), AccessTokenInfo("second-token", 0)]
    )
    credential.close = AsyncMock()
    managed_identity = MagicMock(return_value=credential)
    monkeypatch.setattr(azure_openai, "ManagedIdentityCredential", managed_identity)

    headers = []

    def respond(request):
        headers.append(request.headers)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-deployment",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    clients = []

    def make_client(**kwargs):
        client = AsyncAzureOpenAI(**kwargs, http_client=http_client)
        clients.append(client)
        return client

    monkeypatch.setattr(azure_openai, "AsyncAzureOpenAI", make_client)
    resources = AsyncExitStack()
    try:
        if use_factory:
            await get_llm_client()
        else:
            await AzureOpenAIProvider().get_client(resources)

        for _ in range(2):
            await clients[0].chat.completions.create(
                model="test-deployment", messages=[{"role": "user", "content": "test"}]
            )

        assert not http_client.is_closed
        if api_key:
            assert [item.get("api-key") for item in headers] == [api_key, api_key]
            managed_identity.assert_not_called()
        else:
            assert [item.get("authorization") for item in headers] == ["Bearer first-token", "Bearer second-token"]
            assert credential.get_token_info.await_count == 2
            credential.get_token_info.assert_awaited_with(azure_openai.AZURE_COGNITIVE_SERVICES_SCOPE, options={})
    finally:
        if use_factory:
            await close_llm_clients()
        else:
            await resources.aclose()

    assert http_client.is_closed
    if not api_key:
        credential.close.assert_awaited_once()
