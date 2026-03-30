# Requirements Document

## Introduction

Phase 3 package hygiene for the `context_graph` Python package. This spec addresses five structural issues discovered during a post-phase-2 audit: broken lazy-export paths in `__init__.py`, a misplaced `memory/` package, a duplicated `_arg_or_env` helper, a fragile optional-dependency export in `runtime/llm/__init__.py`, and a dev-only profiling module shipped as production code. The goal is to eliminate dead references, reduce duplication, correct package placement, and harden optional-dependency handling.

## Glossary

- **Top_Level_Init**: The `context_graph/__init__.py` module containing the `_EXPORTS` lazy-loading dictionary.
- **_EXPORTS**: The dictionary in Top_Level_Init that maps public symbol names to `(module_path, attribute_name)` tuples for lazy `__getattr__` loading.
- **Memory_Package**: The `context_graph/memory/` Python package, currently containing only `prompts/models.py` and `prompts/prompt_helpers.py`.
- **Prompt_Types_Module**: The module providing `Message`, `PromptVersion`, and `PromptFunction` types (currently `context_graph/memory/prompts/models.py`).
- **Prompt_Helpers_Module**: The module providing `to_prompt_json` and `DO_NOT_ESCAPE_UNICODE` (currently `context_graph/memory/prompts/prompt_helpers.py`).
- **_arg_or_env**: A helper function `_arg_or_env(args, attr, env_name)` that resolves a value from CLI args or environment variables.
- **LLM_Init**: The `context_graph/runtime/llm/__init__.py` module.
- **Embedding_Clients_Init**: The `context_graph/runtime/embedding_clients/__init__.py` module.
- **Profiling_Package**: The `context_graph/profiling/` Python package containing `selected_functions.py`.
- **Cleanup_Process**: The automated or manual process that applies the changes specified in this document.

## Requirements

### Requirement 1: Fix broken _EXPORTS paths in Top_Level_Init

**User Story:** As a developer, I want all entries in the `_EXPORTS` dictionary to reference valid module paths, so that lazy imports from the top-level package do not raise `ModuleNotFoundError` at runtime.

#### Acceptance Criteria

1. WHEN a symbol in _EXPORTS references `.storage.backend.metadata`, THE Cleanup_Process SHALL replace the module path with `.backend.metadata`.
2. WHEN a symbol in _EXPORTS references `.storage.backend.profile_vectors`, THE Cleanup_Process SHALL replace the module path with `.backend.profile_vectors`.
3. THE Top_Level_Init SHALL contain zero entries in _EXPORTS whose module path includes the substring `.storage.`.
4. WHEN any symbol listed in `__all__` of Top_Level_Init is accessed via `getattr(context_graph, symbol_name)`, THE Top_Level_Init SHALL resolve the import without raising `ModuleNotFoundError` or `AttributeError`.

### Requirement 2: Relocate Memory_Package contents to a top-level prompts package

**User Story:** As a developer, I want prompt message types and helpers to live under a clearly named `prompts/` package rather than the misleading `memory/` package, so that the package structure accurately reflects the contents.

#### Acceptance Criteria

1. THE Cleanup_Process SHALL create a `context_graph/prompts/` package containing the contents currently in `context_graph/memory/prompts/models.py` and `context_graph/memory/prompts/prompt_helpers.py`.
2. THE Cleanup_Process SHALL update all import statements across the codebase that reference `context_graph.memory.prompts.models` to reference the new canonical path under `context_graph.prompts`.
3. THE Cleanup_Process SHALL update all import statements across the codebase that reference `context_graph.memory.prompts.prompt_helpers` to reference the new canonical path under `context_graph.prompts`.
4. WHEN the relocation is complete, THE Cleanup_Process SHALL delete the `context_graph/memory/` package directory entirely (including `__init__.py`, `prompts/__init__.py`, `prompts/models.py`, and `prompts/prompt_helpers.py`).
5. WHEN `from context_graph.prompts.models import Message` is executed, THE `context_graph.prompts` package SHALL resolve the import without error.
6. WHEN `from context_graph.prompts.prompt_helpers import to_prompt_json` is executed, THE `context_graph.prompts` package SHALL resolve the import without error.

### Requirement 3: Extract shared _arg_or_env helper to eliminate duplication

**User Story:** As a developer, I want a single canonical definition of the `_arg_or_env` helper, so that bug fixes and behavior changes only need to happen in one place.

#### Acceptance Criteria

1. THE Cleanup_Process SHALL create a shared utility module (e.g., `context_graph/runtime/_env_utils.py`) containing a single definition of the `_arg_or_env` function.
2. THE Cleanup_Process SHALL update `context_graph/runtime/embeddings.py` to import `_arg_or_env` from the shared utility module instead of defining it locally.
3. THE Cleanup_Process SHALL update `context_graph/runtime/llm_config.py` to import `_arg_or_env` from the shared utility module instead of defining it locally.
4. THE Cleanup_Process SHALL remove the local `_arg_or_env` function definitions from both `context_graph/runtime/embeddings.py` and `context_graph/runtime/llm_config.py`.
5. WHEN `_arg_or_env` is called from either `embeddings.py` or `llm_config.py`, THE shared utility module SHALL provide identical behavior to the original local definitions.

### Requirement 4: Harden BedrockLLMClient optional export in LLM_Init

**User Story:** As a developer, I want `__all__` in `runtime/llm/__init__.py` to accurately reflect what is actually importable, so that IDE autocompletion and `import *` do not advertise symbols that will fail at access time.

#### Acceptance Criteria

1. THE LLM_Init SHALL only include `BedrockLLMClient` in `__all__` when the `boto3` dependency is available and the import succeeds.
2. WHILE `boto3` is not installed, THE LLM_Init SHALL omit `BedrockLLMClient` from `__all__`.
3. THE Cleanup_Process SHALL apply the same conditional-export pattern used in Embedding_Clients_Init (where `__all__` is extended via `+=` only on successful import) to LLM_Init.
4. WHEN `from context_graph.runtime.llm import *` is executed in an environment without `boto3`, THE LLM_Init SHALL not expose `BedrockLLMClient` in the imported namespace.

### Requirement 5: Move Profiling_Package out of the installable package

**User Story:** As a developer, I want the profiling/benchmarking harness excluded from the installable library, so that production deployments do not ship dev-only code with heavyweight transitive imports.

#### Acceptance Criteria

1. THE Cleanup_Process SHALL move `context_graph/profiling/selected_functions.py` to a location outside the `context_graph/` installable package (e.g., `tools/profiling/selected_functions.py` or `scripts/profiling/selected_functions.py`).
2. THE Cleanup_Process SHALL delete the `context_graph/profiling/` package directory (including `__init__.py`).
3. WHEN the `context_graph` package is installed via `pip install`, THE installed distribution SHALL not contain any `profiling` subpackage or `selected_functions` module.
4. THE relocated profiling script SHALL remain executable from the repository root (e.g., via `python -m tools.profiling.selected_functions` or `python scripts/profiling/selected_functions.py`).
5. IF the relocated script requires imports from `context_graph`, THEN THE script SHALL use standard absolute imports that work when `context_graph` is installed or on `PYTHONPATH`.
