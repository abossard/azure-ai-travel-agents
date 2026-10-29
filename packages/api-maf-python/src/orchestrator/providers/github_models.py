"""GitHub Models LLM provider."""

import logging
from contextlib import AsyncExitStack
from typing import Any

from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from src.config import settings
from .base import LLMProvider

logger = logging.getLogger(__name__)


class GitHubModelsProvider(LLMProvider):
    """GitHub Models LLM provider."""

    async def get_client(self, resources: AsyncExitStack | None = None) -> Any:
        """Get GitHub Models chat client for Microsoft Agent Framework.

        Returns:
            OpenAIChatCompletionClient configured for GitHub Models

        Raises:
            ValueError: If required configuration is missing
        """
        logger.info("Using GitHub Models")

        if not settings.github_token:
            raise ValueError("GITHUB_TOKEN is required for github-models provider")

        if not settings.github_model:
            raise ValueError("GITHUB_MODEL is required for github-models provider")

        # Create the underlying OpenAI async client for GitHub Models
        async_client = AsyncOpenAI(
            base_url="https://models.inference.ai.azure.com",
            api_key=settings.github_token,
        )
        if resources is not None:
            resources.push_async_callback(async_client.close)

        # Wrap with MAF's chat-completions client
        maf_client = OpenAIChatCompletionClient(
            model=settings.github_model,
            async_client=async_client,
        )

        logger.info(f"Created MAF OpenAIChatClient with model: {settings.github_model}")
        return maf_client
