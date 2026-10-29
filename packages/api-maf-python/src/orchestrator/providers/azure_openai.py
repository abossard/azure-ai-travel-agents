"""Azure OpenAI LLM provider."""

import logging
from contextlib import AsyncExitStack
from typing import Any

from agent_framework.openai import OpenAIChatCompletionClient
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from src.config import settings
from .base import LLMProvider

logger = logging.getLogger(__name__)

AZURE_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


class AzureOpenAIProvider(LLMProvider):
    """Azure OpenAI LLM provider with Managed Identity support."""

    def __init__(self) -> None:
        self._resources = AsyncExitStack()

    async def close(self) -> None:
        await self._resources.aclose()

    async def get_client(self, resources: AsyncExitStack | None = None) -> Any:
        """Get Azure OpenAI chat client for Microsoft Agent Framework.

        Returns:
            OpenAIChatCompletionClient configured with Azure OpenAI

        Raises:
            ValueError: If required configuration is missing
        """
        logger.info("Using Azure OpenAI")

        if not settings.azure_openai_endpoint:
            raise ValueError("AZURE_OPENAI_ENDPOINT is required for azure-openai provider")

        if not settings.azure_openai_deployment_name:
            raise ValueError("AZURE_OPENAI_DEPLOYMENT_NAME is required for azure-openai provider")

        # Create the underlying Azure OpenAI async client
        async_client: AsyncAzureOpenAI
        resources = resources if resources is not None else self._resources

        # If API key is provided, use it (local development or explicit configuration)
        if settings.azure_openai_api_key:
            logger.info("Using API key authentication")
            async_client = AsyncAzureOpenAI(
                api_key=settings.azure_openai_api_key,
                api_version=settings.azure_openai_api_version,
                azure_endpoint=settings.azure_openai_endpoint,
            )
        else:
            # Otherwise, use Managed Identity in Azure environments
            logger.info("Using Managed Identity authentication")
            credential: DefaultAzureCredential | ManagedIdentityCredential
            if settings.azure_client_id:
                logger.info(f"Using Azure Client ID: {settings.azure_client_id}")
                credential = ManagedIdentityCredential(client_id=settings.azure_client_id)
            else:
                credential = DefaultAzureCredential()
            resources.push_async_callback(credential.close)

            async_client = AsyncAzureOpenAI(
                api_version=settings.azure_openai_api_version,
                azure_endpoint=settings.azure_openai_endpoint,
                azure_ad_token_provider=get_bearer_token_provider(credential, AZURE_COGNITIVE_SERVICES_SCOPE),
            )

        resources.push_async_callback(async_client.close)

        # Wrap the Azure OpenAI client with MAF's chat-completions client
        # This provides the ChatClientProtocol interface that ChatAgent expects
        maf_client = OpenAIChatCompletionClient(
            model=settings.azure_openai_deployment_name,
            async_client=async_client,
        )

        logger.info(f"Created MAF OpenAIChatClient with model: {settings.azure_openai_deployment_name}")
        return maf_client
