# Implementation Plan: Phase 1 — Internal Package Restructuring

## Overview

Reorganize `context_graph/` package internals: consolidate data models into `models/`, relocate misplaced modules into `runtime/` and `query/`, clean up dead references and empty stubs, remove compatibility shims, and update the top-level `__init__.py`. All changes are structural — no business logic changes. User-approved pivot on 2026-03-30: backward compatibility shims may be removed, so only canonical in-repo import paths need to work.

## Tasks

- [x] 1. Create the `models/` sub-package and move data model files
  - [x] 1.1 Create `context_graph/models/` directory with `__init__.py`, move `context_graph/models.py` to `context_graph/models/graph.py`
    - Rename the existing `context_graph/models.py` to `context_graph/models/graph.py`
    - Create `context_graph/models/__init__.py` with a docstring and re-exports of all public symbols from `graph.py`, `entity_nodes.py`, and `profiles.py` (as specified in the design)
    - _Requirements: 1.1, 1.2, 1.5_

  - [x] 1.2 Move `context_graph/entity_nodes.py` to `context_graph/models/entity_nodes.py`
    - Move the file; no internal import changes needed (it has no relative imports to other root-level modules)
    - _Requirements: 1.3_

  - [x] 1.3 Move `context_graph/profiles.py` to `context_graph/models/profiles.py`
    - Move the file
    - Update internal imports: `from .entity_nodes import ...` stays the same (same sub-package), `from .models import canonical_json` becomes `from .graph import canonical_json`
    - The `TYPE_CHECKING` import `from .episodes import ...` references a non-existent module; leave it as-is since it's guarded and uses `Any` at runtime
    - _Requirements: 1.4_

  - [x] 1.4 Verify `models/__init__.py` re-exports all public symbols
    - Ensure `from context_graph.models import CanonicalNode`, `from context_graph.models import EntityNode`, `from context_graph.models import ProfileRecord` all work
    - _Requirements: 1.5, 1.6_

- [x] 2. Checkpoint — Ensure models sub-package is importable
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Move runtime wrapper files into `runtime/`
  - [x] 3.1 Move `context_graph/embeddings.py` to `context_graph/runtime/embeddings.py`
    - Move the file
    - Update internal imports: `from .runtime.embedding.openai import ...` becomes `from .embedding.openai import ...`, `from .runtime.embedding.bedrock import ...` becomes `from .embedding.bedrock import ...`
    - _Requirements: 2.1_

  - [x] 3.2 Move `context_graph/llm.py` to `context_graph/runtime/llm_config.py`
    - Move the file (name `llm_config.py` avoids collision with existing `runtime/llm/` directory)
    - Update internal imports: `from .runtime.llm.config import LLMConfig` becomes `from .llm.config import LLMConfig`, `from .runtime.llm.openai_client import ...` becomes `from .llm.openai_client import ...`, `from .runtime.llm.bedrock_client import ...` becomes `from .llm.bedrock_client import ...`
    - _Requirements: 2.2_

  - [x] 3.3 Update `context_graph/runtime/__init__.py` with docstring and re-exports
    - Add docstring: `"""Runtime configuration, provider factories, and client wrappers."""`
    - Re-export `EmbeddingProvider`, `EmbeddingSettings`, `load_embedding_settings`, `make_embedding_provider` from `.embeddings`
    - Re-export `EpisodeLLMSettings`, `load_episode_llm_settings`, `make_episode_llm_client` from `.llm_config`
    - _Requirements: 2.3_

- [x] 4. Move `query_parser.py` into `query/`
  - [x] 4.1 Move `context_graph/query_parser.py` to `context_graph/query/parser.py`
    - Move the file; no internal import changes needed (it uses no relative imports to root-level modules)
    - _Requirements: 3.1_

  - [x] 4.2 Update `context_graph/query/__init__.py` to re-export parser symbols
    - Add `QueryParser`, `QueryParseResult`, `QueryAnchor` to the existing re-exports and `__all__`
    - Preserve existing docstring and code_graph exports
    - _Requirements: 3.2_

- [x] 5. Update all internal consumer imports
  - [x] 5.1 Update `context_graph/cli.py` imports
    - `from .embeddings import ...` → `from .runtime.embeddings import ...`
    - `from .llm import ...` → `from .runtime.llm_config import ...`
    - `from .query_parser import QueryParser` → `from .query.parser import QueryParser`
    - `from .models import utc_now_iso` (inside `_now_iso()`) → `from .models.graph import utc_now_iso` (or keep as `from .models import utc_now_iso` since models/__init__.py re-exports it)
    - _Requirements: 2.5, 3.4_

  - [x] 5.2 Update `context_graph/profile_vectors.py` imports
    - `from .embeddings import EmbeddingProvider` → `from .runtime.embeddings import EmbeddingProvider`
    - `from .profiles import ...` → `from .models.profiles import ...`
    - `from .query_parser import ...` → `from .query.parser import ...`
    - _Requirements: 2.4, 3.4_

  - [x] 5.3 Update `context_graph/provenance/claim_retrieval.py` imports
    - `from context_graph.query_parser import ...` → `from context_graph.query.parser import ...`
    - `from context_graph.embeddings import ...` (TYPE_CHECKING) → `from context_graph.runtime.embeddings import ...`
    - `from context_graph.profiles import ...` (TYPE_CHECKING and runtime) → `from context_graph.models.profiles import ...`
    - _Requirements: 2.4, 3.3_

  - [x] 5.4 Update `context_graph/provenance/*.py` imports (multiple files)
    - `provenance/grounding.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`
    - `provenance/evidence_planner.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`
    - `provenance/validation.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`
    - `provenance/observation_ingestion.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`; `from context_graph.profiles import ...` → `from context_graph.models.profiles import ...`
    - `provenance/claim_planner.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`; `from context_graph.profiles import ...` → `from context_graph.models.profiles import ...`
    - `provenance/invalidation.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`; `from context_graph.profiles import ...` → `from context_graph.models.profiles import ...`
    - `provenance/source_adapters.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`
    - `provenance/planner.py`: `from context_graph.models import GraphSnapshot` → `from context_graph.models.graph import GraphSnapshot`
    - `provenance/prompt_context.py`: `from context_graph.models import ...` → `from context_graph.models.graph import ...`
    - _Requirements: 1.6, 2.4_

  - [x] 5.5 Update `context_graph/storage/backend/metadata/store.py` imports
    - `from context_graph.models import ...` → `from context_graph.models.graph import ...`
    - `from context_graph.profiles import ...` → `from context_graph.models.profiles import ...`
    - _Requirements: 1.6_

  - [x] 5.6 Update `context_graph/storage/backend/profile_vectors/store.py` imports
    - `from context_graph.profiles import ProfileDeactivation` → `from context_graph.models.profiles import ProfileDeactivation`
    - _Requirements: 1.6_

  - [x] 5.7 Update `context_graph/query/code_graph.py` imports
    - `from context_graph.models import ...` → `from context_graph.models.graph import ...`
    - _Requirements: 1.6_

  - [x] 5.8 Update `context_graph/code_identity/resolver.py` imports
    - `from context_graph.models import ...` → `from context_graph.models.graph import ...`
    - _Requirements: 1.6_

  - [x] 5.9 Update `context_graph/profiling/selected_functions.py` imports
    - `from context_graph.models import ...` → `from context_graph.models.graph import ...`
    - _Requirements: 1.6_

- [x] 6. Checkpoint — Ensure all internal imports resolve correctly
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Remove compatibility shims
  - [x] 7.1 Delete pure shim modules once repo-local imports use canonical paths
    - Removed `context_graph/metadata.py`, `context_graph/embeddings.py`, `context_graph/llm.py`, `context_graph/query_parser.py`, `context_graph/profiles.py`, and `context_graph/entity_nodes.py`
    - _Requirements: 4.1, 4.3_

  - [x] 7.2 Convert `context_graph/profile_vectors.py` into a canonical service layer
    - Removed backend-store/config re-export surface and imported canonical backend types directly
    - _Requirements: 4.2, 4.3_

- [x] 8. Remove dead references from `__init__.py` and update `_EXPORTS`
  - [x] 8.1 Remove dead `_EXPORTS` entries pointing to non-existent modules
    - Remove entries for: `BaseCypherExecutor`, `CypherStatement`, `GraphitiNeo4jExecutor`, `GraphitiNeptuneExecutor`, `Neo4jGraphSink`, `NeptuneGraphSink` (all from `.legacy_graph_backend`)
    - Remove entries for: `EntityEdge`, `EpisodicNode`, `MentionRecord` (from `.episodes`)
    - Remove entries for: `EpisodeBatchResult`, `EpisodeProcessor`, `EpisodeWorkItem` (from `.episode_processor`)
    - Remove entry for: `GraphReadRepository` (from `.graph_read`)
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 8.2 Update remaining `_EXPORTS` entries to use new canonical paths
    - `CanonicalEdge`, `CanonicalNode`, `EdgeChange`, `GraphSnapshot`, `NodeChange`, `SnapshotApplyResult`, `SnapshotDiff`: change from `(".models", ...)` to `(".models.graph", ...)`
    - `EntityNode`: change from `(".episodes", "EntityNode")` to `(".models.entity_nodes", "EntityNode")`
    - `ProfileDeactivation`, `ProfileRecord`, `SEMANTIC_*_PROFILE_KIND`: change from `(".profiles", ...)` to `(".models.profiles", ...)`
    - `EmbeddingProvider`, `EmbeddingSettings`: change from `(".embeddings", ...)` to `(".runtime.embeddings", ...)`
    - `QueryAnchor`, `QueryParseResult`, `QueryParser`: change from `(".query_parser", ...)` to `(".query.parser", ...)`
    - `AuroraMetadataStore`, `InMemoryMetadataStore`, `RepoIndexState`, `RepoSourceState`, `SQLiteMetadataStore`: change from `(".metadata", ...)` to `(".storage.backend.metadata", ...)` (canonical path over shim)
    - Update `__all__` to match the new `_EXPORTS` keys (dead symbols removed)
    - _Requirements: 5.4, 4.4_

- [x] 9. Remove empty packages and directories
  - [x] 9.1 Remove `context_graph/server/` directory
    - Delete `context_graph/server/__init__.py` and the `server/` directory (empty package, no references)
    - _Requirements: 6.2_

  - [x] 9.2 Remove `context_graph/storage/graph/` directory
    - Delete the empty `storage/graph/` directory (no references)
    - _Requirements: 6.1_

  - [x] 9.3 Add docstring to `context_graph/storage/__init__.py`
    - Add `"""Persistence backends and metadata storage for the context graph."""`
    - _Requirements: 6.3_

- [x] 10. Add docstrings to all `__init__.py` files missing them
  - [x] 10.1 Add docstrings to `__init__.py` files that currently lack them
    - `context_graph/runtime/embedding/__init__.py`: Add `"""Embedding client implementations for Bedrock and OpenAI."""` (preserve existing code)
    - `context_graph/runtime/providers/__init__.py`: Add `"""High-level factory helpers for database and AI provider construction."""`
    - `context_graph/ingestion/__init__.py`: Add `"""Git-aware codebase ingestion, snapshot building, and diffing."""` at the top (before `from __future__`)
    - `context_graph/ingestion/git/__init__.py`: Add `"""Git metadata extraction and GitHub integration for ingestion."""`
    - `context_graph/memory/prompts/__init__.py`: Add `"""Prompt message models and helpers for memory operations."""` (before existing imports)
    - Verify existing docstrings are preserved in: `code_identity/`, `memory/`, `profiling/`, `provenance/`, `provenance/prompts/`, `query/`, `runtime/llm/`, `storage/backend/`
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 11. Remove dead code from `cli.py`
  - [x] 11.1 Remove `_make_episode_processor()` function from `context_graph/cli.py`
    - This function imports from `.episode_processor` and `.graph_read` which don't exist
    - It is defined but never called anywhere in the codebase
    - Remove the entire function definition
    - _Requirements: 5.2_

- [x] 12. Checkpoint — Full structural verification
  - Ensure all tests pass, ask the user if questions arise.

- [ ]* 13. Write property-based tests for correctness properties
  - [ ]* 13.1 Write property test: All exported symbols are importable
    - **Property 1: All exported symbols are importable**
    - For any symbol in `context_graph.__all__`, `getattr(context_graph, name)` succeeds without error and is not `None`
    - **Validates: Requirements 5.1, 5.3, 5.4**

  - [ ]* 13.2 Write property test: Removed shim modules stay absent
    - **Property 2: Removed shim modules stay absent**
    - Define the deleted legacy module paths and assert importing each one raises `ModuleNotFoundError`
    - **Validates: Breaking-cleanup pivot from requirements 1.6, 2.4, 3.3**

  - [ ]* 13.3 Write property test: No circular imports
    - **Property 3: No circular imports**
    - Collect all `.py` files under `context_graph/`, convert to module paths, `importlib.import_module` each, assert no `ImportError`
    - **Validates: Requirements 1.1, 2.1, 3.1**

  - [ ]* 13.4 Write property test: All `__init__.py` files have docstrings
    - **Property 4: All __init__.py files have docstrings**
    - Walk `context_graph/` directory tree, find all `__init__.py`, import corresponding module, assert `module.__doc__` is non-empty
    - **Validates: Requirements 7.1, 7.3**

  - [ ]* 13.5 Write property test: `models/` sub-package re-exports all child symbols
    - **Property 5: models/ sub-package re-exports all child symbols**
    - Collect public names from `models.graph`, `models.entity_nodes`, `models.profiles`; verify each is accessible via `context_graph.models` and is the same object
    - **Validates: Requirements 1.1, 1.5**

  - [ ]* 13.6 Write property test: `_EXPORTS` uses canonical paths over shim paths
    - **Property 6: _EXPORTS uses canonical paths over shim paths**
    - Inspect `_EXPORTS` dict, filter entries whose symbols exist in `storage.backend.metadata`, assert module path is `.storage.backend.metadata`
    - **Validates: Requirements 4.4**

- [x] 14. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation after each major structural change
- Property tests validate universal correctness properties from the design document
- The design uses Python — no language selection was needed
- External consumers (`mcp_server/`, `tests/`) now import canonical package paths directly; top-level `_EXPORTS` and `models/__init__.py` remain the supported public surface
