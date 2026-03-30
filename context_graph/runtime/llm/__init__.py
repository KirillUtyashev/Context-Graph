"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "BedrockLLMClient": (".bedrock_client", "BedrockLLMClient"),
    "LLMClient": (".client", "LLMClient"),
    "LLMConfig": (".config", "LLMConfig"),
    "OpenAIClient": (".openai_client", "OpenAIClient"),
    "RateLimitError": (".errors", "RateLimitError"),
    "TokenUsage": (".token_tracker", "TokenUsage"),
    "TokenUsageTracker": (".token_tracker", "TokenUsageTracker"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name, __name__)
    return getattr(module, attr_name)


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
