"""Docker Models LLM provider."""

import logging
from contextlib import AsyncExitStack
from typing import Any

from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from src.config import settings
from .base import LLMProvider

logger = logging.getLogger(__name__)


class DockerModelsProvider(LLMProvider):
    """Docker Models LLM provider."""

    async def get_client(self, resources: AsyncExitStack | None = None) -> Any:
        """Get Docker Models chat client for Microsoft Agent Framework.

        Returns:
            OpenAIChatCompletionClient configured for Docker Models

        Raises:
            ValueError: If required configuration is missing
        """
        logger.info("Using Docker Models")

        if not settings.docker_model_endpoint:
            raise ValueError("DOCKER_MODEL_ENDPOINT is required for docker-models provider")

        if not settings.docker_model:
            raise ValueError("DOCKER_MODEL is required for docker-models provider")

        # Create the underlying OpenAI async client for Docker Models
        async_client = AsyncOpenAI(
            base_url=settings.docker_model_endpoint,
            api_key="DOCKER_API_KEY",  # Placeholder API key for Docker models
        )
        if resources is not None:
            resources.push_async_callback(async_client.close)

        # Wrap with MAF's chat-completions client
        maf_client = OpenAIChatCompletionClient(
            model=settings.docker_model,
            async_client=async_client,
        )

        logger.info(f"Created MAF OpenAIChatClient with model: {settings.docker_model}")
        return maf_client
