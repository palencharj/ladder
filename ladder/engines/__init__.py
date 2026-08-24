"""Engine registry."""

from .anthropic_engine import AnthropicEngine
from .base import Engine, Result, Timer
from .cli_engine import ClaudeCliEngine
from .ollama_engine import OllamaEngine

__all__ = [
    "Engine", "Result", "Timer",
    "OllamaEngine", "AnthropicEngine", "ClaudeCliEngine",
]
