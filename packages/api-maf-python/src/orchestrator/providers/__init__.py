"""LLM provider factory using strategy pattern.

This module provides a factory function to get the appropriate LLM client
based on the LLM_PROVIDER environment variable, implementing the same
strategy pattern as the TypeScript implementation.
"""

import logging
from contextlib import AsyncExitStack
from typing import Any

from src.config import settings
from .azure_openai import AzureOpenAIProvider
from .docker_models import DockerModelsProvider
from .foundry_local import FoundryLocalProvider
from .github_models import GitHubModelsProvider
from .ollama_models import OllamaModelsProvider

logger = logging.getLogger(__name__)

_default_resources = AsyncExitStack()


async def get_llm_client(resources: AsyncExitStack | None = None) -> Any:
    """Get LLM client based on configured provider.

    Args:
        resources: Optional stack that owns the client resources. If omitted,
            call close_llm_clients when the client is no longer needed.

    Returns:
        Configured LLM client instance

    Raises:
        ValueError: If provider is unknown or misconfigured
    """
    provider = settings.llm_provider
    resources = resources if resources is not None else _default_resources

    logger.info(f"Initializing LLM provider: {provider}")

    if provider == "azure-openai":
        provider_instance = AzureOpenAIProvider()
        return await provider_instance.get_client(resources)

    elif provider == "github-models":
        provider_instance = GitHubModelsProvider()
        return await provider_instance.get_client(resources)

    elif provider == "docker-models":
        provider_instance = DockerModelsProvider()
        return await provider_instance.get_client(resources)

    elif provider == "ollama-models":
        provider_instance = OllamaModelsProvider()
        return await provider_instance.get_client(resources)

    elif provider == "foundry-local":
        provider_instance = FoundryLocalProvider()
        return await provider_instance.get_client()

    else:
        raise ValueError(
            f'Unknown LLM_PROVIDER "{provider}". '
            "Valid options are: azure-openai, github-models, docker-models, "
            "ollama-models, foundry-local."
        )


async def close_llm_clients() -> None:
    """Close clients created without an explicit resource stack."""
    await _default_resources.aclose()


__all__ = ["get_llm_client", "close_llm_clients"]
