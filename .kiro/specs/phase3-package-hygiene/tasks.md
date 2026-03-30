# Implementation Plan: Phase 3 Package Hygiene

## Overview

Refactoring-only changes to fix five structural issues in the `context_graph` package: broken lazy-export paths, misplaced `memory/` package, duplicated `_arg_or_env` helper, fragile `BedrockLLMClient` export, and dev-only `profiling/` shipped as production code. Each task is incremental and builds on the previous.

## Tasks

- [x] 1. Fix broken `_EXPORTS` paths in `context_graph/__init__.py`
  - [x] 1.1 Replace all `.storage.backend.metadata` references with `.backend.metadata` and `.storage.backend.profile_vectors` with `.backend.profile_vectors` in the `_EXPORTS` dict
    - Fix 8 entries: `AuroraMetadataStore`, `InMemoryMetadataStore`, `RepoIndexState`, `RepoSourceState`, `SQLiteMetadataStore`, `AuroraProfileVectorStore`, `InMemoryProfileVectorStore`, `ProfileVectorHit`
    - Verify zero entries contain the substring `.storage.`
    - _Requirements: 1.1, 1.2, 1.3_

  - [ ]* 1.2 Write property test: all lazy-export symbols resolve
    - **Property 1: All lazy-export symbols resolve**
    - Iterate all symbols in `context_graph.__all__` and assert `getattr(context_graph, name)` does not raise `ModuleNotFoundError` or `AttributeError`
    - **Validates: Requirements 1.3, 1.4**

- [x] 2. Checkpoint — Verify `_EXPORTS` fix
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Relocate `memory/` package contents to `context_graph/prompts/`
  - [x] 3.1 Create `context_graph/prompts/` package with `__init__.py`, `models.py`, and `prompt_helpers.py`
    - Copy contents from `context_graph/memory/prompts/models.py` → `context_graph/prompts/models.py`
    - Copy contents from `context_graph/memory/prompts/prompt_helpers.py` → `context_graph/prompts/prompt_helpers.py`
    - Create `context_graph/prompts/__init__.py` re-exporting `Message` (matching current `memory/prompts/__init__.py`)
    - _Requirements: 2.1_

  - [x] 3.2 Update all import statements referencing `context_graph.memory.prompts`
    - Update `context_graph/runtime/llm/client.py`
    - Update `context_graph/runtime/llm/openai_base_client.py`
    - Update `context_graph/runtime/llm/bedrock_client.py`
    - Update `context_graph/provenance/services.py`
    - Update `context_graph/provenance/prompts/base.py`
    - Update `context_graph/provenance/prompts/extract_claims_and_evidence.py`
    - Update `context_graph/provenance/prompts/normalize_claims.py`
    - Update `context_graph/provenance/prompts/relate_claims.py`
    - Update `context_graph/provenance/prompts/resolve_fact_conflicts.py`
    - Update `context_graph/provenance/prompts/synthesize_research_artifact.py`
    - Change `context_graph.memory.prompts.models` → `context_graph.prompts.models`
    - Change `context_graph.memory.prompts.prompt_helpers` → `context_graph.prompts.prompt_helpers`
    - _Requirements: 2.2, 2.3_

  - [x] 3.3 Delete the `context_graph/memory/` directory entirely
    - Remove `context_graph/memory/__init__.py`, `context_graph/memory/prompts/__init__.py`, `context_graph/memory/prompts/models.py`, `context_graph/memory/prompts/prompt_helpers.py`
    - _Requirements: 2.4_

  - [ ]* 3.4 Write property test: no stale memory import paths
    - **Property 2: No stale memory import paths**
    - Walk all `.py` files under `context_graph/` and assert none contain `context_graph.memory` as an import target
    - **Validates: Requirements 2.2, 2.3**

- [x] 4. Checkpoint — Verify memory relocation
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Extract shared `_arg_or_env` helper
  - [x] 5.1 Create `context_graph/runtime/_env_utils.py` with the canonical `_arg_or_env` function
    - Use the stricter `embeddings.py` semantics: `if value is not None and str(value)`
    - _Requirements: 3.1_

  - [x] 5.2 Update `context_graph/runtime/embeddings.py` to import `_arg_or_env` from `_env_utils`
    - Remove the local `_arg_or_env` definition
    - Add `from context_graph.runtime._env_utils import _arg_or_env`
    - _Requirements: 3.2, 3.4_

  - [x] 5.3 Update `context_graph/runtime/llm_config.py` to import `_arg_or_env` from `_env_utils`
    - Remove the local `_arg_or_env` definition
    - Add `from context_graph.runtime._env_utils import _arg_or_env`
    - _Requirements: 3.3, 3.4_

  - [ ]* 5.4 Write property test: `_arg_or_env` behavioral equivalence
    - **Property 3: `_arg_or_env` behavioral equivalence**
    - Use Hypothesis to generate random `(args, attr, env_name)` tuples and assert the shared implementation matches the expected resolution logic
    - **Validates: Requirements 3.5**

- [x] 6. Harden `BedrockLLMClient` conditional export in `runtime/llm/__init__.py`
  - [x] 6.1 Update `context_graph/runtime/llm/__init__.py` to conditionally add `BedrockLLMClient` to `__all__`
    - Remove `BedrockLLMClient` from the static `__all__` list
    - Add `__all__ += ['BedrockLLMClient']` inside the existing `try` block after the successful import
    - Mirror the pattern used in `context_graph/runtime/embedding_clients/__init__.py`
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 7. Checkpoint — Verify helper extraction and LLM init hardening
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Move `profiling/` package out of installable package
  - [x] 8.1 Create `tools/profiling/` directory with `__init__.py` and move `selected_functions.py`
    - Create `tools/__init__.py` and `tools/profiling/__init__.py` to make it importable
    - Move `context_graph/profiling/selected_functions.py` → `tools/profiling/selected_functions.py`
    - _Requirements: 5.1_

  - [x] 8.2 Update `scripts/profile_selected_functions.py` to import from `tools.profiling`
    - Change `from context_graph.profiling.selected_functions import main` → `from tools.profiling.selected_functions import main`
    - _Requirements: 5.4, 5.5_

  - [x] 8.3 Update `tests/profiling/test_selected_function_profiling.py` to import from `tools.profiling`
    - Change `from context_graph.profiling.selected_functions import ALL_TARGETS, run_profiles` → `from tools.profiling.selected_functions import ALL_TARGETS, run_profiles`
    - _Requirements: 5.4, 5.5_

  - [x] 8.4 Delete `context_graph/profiling/` directory entirely
    - Remove `context_graph/profiling/__init__.py` and `context_graph/profiling/selected_functions.py`
    - _Requirements: 5.2, 5.3_

  - [ ]* 8.5 Write property test: no dev-only modules in installable package
    - **Property 4: No dev-only modules in installable package**
    - Walk all paths under `context_graph/` and assert none contain a `profiling` directory component
    - **Validates: Requirements 5.3**

- [x] 9. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each logical group of changes
- Property tests validate universal correctness properties from the design document
- All changes are refactoring-only — no new features or behavioral changes
