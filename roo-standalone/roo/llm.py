"""
Model-Agnostic LLM Client

Supports multiple LLM providers with a unified async interface.
"""
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum
import base64

from .config import get_settings


class LLMProvider(str, Enum):
    """Supported LLM providers."""
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


@dataclass
class LLMResponse:
    """Standardized response from LLM."""
    content: str
    model: str
    usage: Optional[Dict[str, int]] = None


@dataclass
class ToolCall:
    """A single tool/function call chosen by the model."""
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    model: str = ""


class ToolCallParseError(ValueError):
    """The model produced no tool call or unparsable arguments."""


class BaseLLMClient(ABC):
    """Abstract base class for LLM clients."""

    @abstractmethod
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        """Send a chat completion request."""
        pass

    @abstractmethod
    async def embed(self, text: str) -> List[float]:
        """Generate embeddings for text."""
        pass

    async def chat_tools(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        **kwargs,
    ) -> ToolCall:
        """Send a chat request that must answer with a tool call."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support tool calling yet; "
            "use an OpenAI-compatible provider (openai/gemini) for the router."
        )


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI API (also used for Gemini via compatibility layer)."""
    
    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None):
        from openai import AsyncOpenAI
        
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        """Send chat completion request."""
        model = kwargs.get("model", self.model)
        # Newer OpenAI models (gpt-5+, o-series) have different param requirements
        is_reasoning_model = model.startswith(("gpt-5", "o1", "o3"))
        max_tokens_key = "max_completion_tokens" if is_reasoning_model else "max_tokens"
        create_kwargs = {
            "model": model,
            "messages": messages,
            max_tokens_key: kwargs.get("max_tokens", 2048),
        }
        # Reasoning models only support temperature=1 (the default), so omit it
        if not is_reasoning_model:
            create_kwargs["temperature"] = kwargs.get("temperature", 0.7)
        if "extra_body" in kwargs:
            create_kwargs["extra_body"] = kwargs["extra_body"]
        if "reasoning_effort" in kwargs:
            create_kwargs["reasoning_effort"] = kwargs["reasoning_effort"]

        response = await self.client.chat.completions.create(**create_kwargs)
        
        return LLMResponse(
            content=response.choices[0].message.content or "",
            model=response.model,
            usage={
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0
            }
        )
    
    async def chat_tools(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        **kwargs,
    ) -> ToolCall:
        """Chat completion that must answer with a tool call.

        Works for OpenAI and for Gemini through the OpenAI-compatibility layer.
        Raises ToolCallParseError when the model returns no/unparsable tool call
        (callers retry once, then fall back).
        """
        model = kwargs.get("model", self.model)
        is_reasoning_model = model.startswith(("gpt-5", "o1", "o3"))
        max_tokens_key = "max_completion_tokens" if is_reasoning_model else "max_tokens"
        # Reasoning models spend completion tokens on internal reasoning before
        # emitting the tool call — give them headroom or they return nothing.
        default_max_tokens = 4096 if is_reasoning_model else 1024
        create_kwargs = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": kwargs.get("tool_choice", "required"),
            max_tokens_key: kwargs.get("max_tokens", default_max_tokens),
        }
        if not is_reasoning_model:
            create_kwargs["temperature"] = kwargs.get("temperature", 0.2)
        # gpt-5.x rejects function tools + reasoning_effort on /v1/chat/completions
        # (API error 400: "use /v1/responses instead"), so only forward the knob
        # for models that accept the combination.
        if "reasoning_effort" in kwargs and not model.startswith("gpt-5"):
            create_kwargs["reasoning_effort"] = kwargs["reasoning_effort"]

        response = await self.client.chat.completions.create(**create_kwargs)

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            raise ToolCallParseError(
                f"model returned no tool call (content={message.content!r:.200})"
            )
        call = tool_calls[0]
        raw_arguments = call.function.arguments or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ToolCallParseError(
                f"unparsable tool arguments for {call.function.name}: {raw_arguments[:200]}"
            ) from exc
        if not isinstance(arguments, dict):
            raise ToolCallParseError(
                f"tool arguments for {call.function.name} are not an object: {raw_arguments[:200]}"
            )
        return ToolCall(name=call.function.name, arguments=arguments, model=response.model)

    async def embed(self, text: str) -> List[float]:
        """Generate embeddings using OpenAI."""
        response = await self.client.embeddings.create(
            model="text-embedding-ada-002",
            input=text
        )
        return response.data[0].embedding


class AnthropicClient(BaseLLMClient):
    """Client for Anthropic Claude API."""
    
    def __init__(self, api_key: str, model: str = "claude-3-5-sonnet-20241022"):
        from anthropic import AsyncAnthropic
        
        self.model = model
        self.client = AsyncAnthropic(api_key=api_key)
    
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
        """Send chat completion request to Claude."""
        # Extract system message if present
        system = None
        chat_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                chat_messages.append(msg)
        
        response = await self.client.messages.create(
            model=kwargs.get("model", self.model),
            max_tokens=kwargs.get("max_tokens", 2048),
            system=system or "You are a helpful assistant.",
            messages=chat_messages
        )
        
        return LLMResponse(
            content=response.content[0].text if response.content else "",
            model=response.model,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens
            }
        )
    
    async def embed(self, text: str) -> List[float]:
        """Claude doesn't support embeddings, fall back to OpenAI."""
        settings = get_settings()
        if settings.OPENAI_API_KEY:
            client = OpenAIClient(settings.OPENAI_API_KEY, "text-embedding-ada-002")
            return await client.embed(text)
        raise ValueError("OpenAI API key required for embeddings with Anthropic")


# Default configurations
DEFAULT_CONFIGS = {
    LLMProvider.GEMINI: {
        "model": "gemini-2.5-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    LLMProvider.OPENAI: {
        "model": "gpt-4o-mini",
        "base_url": None,
    },
    LLMProvider.ANTHROPIC: {
        "model": "claude-3-5-sonnet-20241022",
        "base_url": None,
    },
}


def get_llm_client(provider: Optional[str] = None) -> BaseLLMClient:
    """
    Factory function to get an LLM client.
    
    Args:
        provider: Provider name ("gemini", "openai", "anthropic")
                  Auto-detects based on available API keys if not specified
    
    Returns:
        LLM client instance
    """
    settings = get_settings()
    
    # Auto-detect provider if not specified
    if provider is None:
        provider = settings.default_llm_provider
    
    provider_enum = LLMProvider(provider.lower())
    config = DEFAULT_CONFIGS[provider_enum]
    
    if provider_enum == LLMProvider.GEMINI:
        if not settings.GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY not configured")
        return OpenAIClient(
            api_key=settings.GOOGLE_API_KEY,
            model=config["model"],
            base_url=config["base_url"]
        )
    
    if provider_enum == LLMProvider.OPENAI:
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY not configured")
        return OpenAIClient(
            api_key=settings.OPENAI_API_KEY,
            model=config["model"]
        )
    
    if provider_enum == LLMProvider.ANTHROPIC:
        if not settings.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY not configured")
        return AnthropicClient(
            api_key=settings.ANTHROPIC_API_KEY,
            model=config["model"]
        )
    
    raise ValueError(f"Unknown provider: {provider}")


# Singleton client
_default_client: Optional[BaseLLMClient] = None


def get_default_client() -> BaseLLMClient:
    """Get or create the default LLM client."""
    global _default_client
    if _default_client is None:
        _default_client = get_llm_client()
    return _default_client


async def chat(messages: List[Dict[str, str]], **kwargs) -> LLMResponse:
    """Convenience function for quick chat completions."""
    client = get_default_client()
    return await client.chat(messages, **kwargs)


async def chat_tools(
    messages: List[Dict[str, str]],
    tools: List[Dict[str, Any]],
    **kwargs,
) -> ToolCall:
    """Convenience function for tool-calling completions (router v2)."""
    client = get_default_client()
    return await client.chat_tools(messages, tools, **kwargs)


async def embed(text: str) -> List[float]:
    """Convenience function for generating embeddings."""
    client = get_default_client()
    return await client.embed(text)


async def extract_text_from_image(
    *,
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    model: Optional[str] = None,
) -> str:
    """Extract structured text from an image using an OpenAI vision-capable model."""
    from openai import AsyncOpenAI

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY not configured")

    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{image_base64}"
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=model or settings.OPENAI_VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        max_tokens=2048,
    )
    return response.choices[0].message.content or ""
