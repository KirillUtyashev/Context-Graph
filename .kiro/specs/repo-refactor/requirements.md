# Requirements Document

## Introduction

This document captures the requirements for refactoring the Context Graph repository in two phases:

**Phase 1 — Internal Package Restructuring** (Requirements 1–7): Reorganize the `context_graph/` Python package internals — consolidate scattered data models, relocate misplaced modules into their correct sub-packages, clean up dead references and empty stubs, and update the top-level `__init__.py` to reflect the new layout while preserving backward compatibility.

**Phase 2 — Repository-Level Cleanup** (Requirements 8–19): Broader housekeeping including moving non-source files out of the package, restructuring tests, improving documentation, adding linting/CI, and making the evaluation suite functional.

### Current State (Phase 1 context)

The `context_graph/` package has several internal organizational issues:
- Data model files (`models.py`, `entity_nodes.py`, `profiles.py`) are scattered at the package root with no grouping
- Configuration/runtime wrapper files (`embeddings.py`, `llm.py`) sit at the package root despite an existing `runtime/` sub-package with `runtime/embedding/` and `runtime/llm/` directories
- Query-related files (`query_parser.py`) sit at the package root despite an existing `query/` sub-package
- Compatibility shim files (`metadata.py`, `profile_vectors.py`) re-export from `storage.backend` but live at the package root with no documentation of their shim status
- The top-level `__init__.py` references modules that no longer exist on disk (`episodes.py`, `episode_processor.py`, `legacy_graph_backend`, `graph_read.py`)
- Empty stubs exist: `server/` (empty package), `storage/graph/` (empty directory), `storage/__init__.py` (empty file)
- Most sub-package `__init__.py` files lack docstrings

## Glossary

- **Package**: The `context_graph/` Python package containing library source code
- **Models_Subpackage**: The new `context_graph/models/` sub-package consolidating data model files
- **Runtime_Subpackage**: The existing `context_graph/runtime/` sub-package for config and provider wrappers
- **Query_Subpackage**: The existing `context_graph/query/` sub-package for query-related modules
- **Compatibility_Shim**: A module that re-exports symbols from their canonical location to preserve old import paths
- **Top_Level_Init**: The `context_graph/__init__.py` file that defines the package's public API via lazy exports
- **Refactoring_Process**: The set of file moves, deletions, and reorganizations applied to the repository
- **Repository**: The Context Graph Git repository and its file tree
- **MCP_App**: The `mcp_server/` standalone MCP application package
- **CI_Pipeline**: The GitHub Actions workflows in `.github/workflows/`
- **Evaluation_Suite**: The `evaluation/` directory containing benchmarking and evaluation code
- **Infra_Directory**: The `infra/` directory containing Terraform infrastructure-as-code
- **Test_Suite**: The `tests/` directory containing pytest test files for the core library
- **Docs_Directory**: The `docs/` directory containing project documentation

## Requirements

---

## Phase 1: Internal Package Restructuring

---

### Requirement 1: Consolidate Data Model Files into a Models Sub-Package

**User Story:** As a developer, I want all core data model definitions grouped under a single `models/` sub-package, so that I can find and maintain data types in one place instead of hunting across scattered root-level files.

#### Acceptance Criteria

1. WHEN the Refactoring_Process is applied, THE Package SHALL contain a `context_graph/models/` sub-package with an `__init__.py` that re-exports all public symbols from its child modules
2. WHEN `context_graph/models.py` exists as a root-level file, THE Refactoring_Process SHALL move it to `context_graph/models/graph.py` (containing CanonicalNode, CanonicalEdge, GraphSnapshot, SnapshotDiff, SnapshotApplyResult, NodeChange, EdgeChange, and utility functions)
3. WHEN `context_graph/entity_nodes.py` exists as a root-level file, THE Refactoring_Process SHALL move it to `context_graph/models/entity_nodes.py` (containing EntityNode and its helper functions)
4. WHEN `context_graph/profiles.py` exists as a root-level file, THE Refactoring_Process SHALL move it to `context_graph/models/profiles.py` (containing ProfileRecord, ProfileDeactivation, ProfileKeyDefinition, and semantic profile factory functions)
5. THE Models_Subpackage `__init__.py` SHALL re-export all previously public symbols so that `from context_graph.models import CanonicalNode` continues to work
6. IF any module outside the Package imports directly from `context_graph.models`, `context_graph.entity_nodes`, or `context_graph.profiles`, THEN THE Refactoring_Process SHALL ensure those import paths remain functional via the new `__init__.py` re-exports or compatibility shims

### Requirement 2: Move Configuration and Runtime Wrapper Files into the Runtime Sub-Package

**User Story:** As a developer, I want embedding and LLM configuration wrappers to live inside the `runtime/` sub-package alongside the provider implementations they configure, so that the package root is not cluttered with runtime plumbing.

#### Acceptance Criteria

1. WHEN `context_graph/embeddings.py` exists at the package root, THE Refactoring_Process SHALL move it to `context_graph/runtime/embeddings.py`
2. WHEN `context_graph/llm.py` exists at the package root, THE Refactoring_Process SHALL move it to `context_graph/runtime/llm_config.py` (avoiding collision with the existing `runtime/llm/` directory)
3. THE Runtime_Subpackage `__init__.py` SHALL re-export the public symbols from the moved modules (EmbeddingSettings, EmbeddingProvider, load_embedding_settings, make_embedding_provider, EpisodeLLMSettings, load_episode_llm_settings, make_episode_llm_client)
4. WHEN other modules import from `context_graph.embeddings` or `context_graph.llm`, THE Refactoring_Process SHALL update those import paths to use `context_graph.runtime.embeddings` and `context_graph.runtime.llm_config`, or provide backward-compatible shims at the old paths
5. THE Refactoring_Process SHALL verify that `cli.py` and any other internal consumers are updated to use the new import paths

### Requirement 3: Move Query-Related Files into the Query Sub-Package

**User Story:** As a developer, I want query parsing logic to live inside the `query/` sub-package alongside the other query modules, so that all retrieval-related code is co-located.

#### Acceptance Criteria

1. WHEN `context_graph/query_parser.py` exists at the package root, THE Refactoring_Process SHALL move it to `context_graph/query/parser.py`
2. THE Query_Subpackage `__init__.py` SHALL re-export the public symbols from the moved module (QueryParser, QueryParseResult, QueryAnchor)
3. WHEN other modules import from `context_graph.query_parser`, THE Refactoring_Process SHALL update those import paths to use `context_graph.query.parser`, or provide a backward-compatible shim at the old path
4. THE Refactoring_Process SHALL verify that `cli.py`, `profile_vectors.py`, and any other internal consumers are updated to use the new import paths

### Requirement 4: Document and Consolidate Compatibility Shims

**User Story:** As a developer, I want compatibility shim modules to be clearly documented as shims with deprecation guidance, so that I know which import paths are canonical and which are legacy.

#### Acceptance Criteria

1. WHEN `context_graph/metadata.py` exists as a Compatibility_Shim re-exporting from `storage.backend.metadata`, THE Refactoring_Process SHALL add a module-level docstring that explicitly states the module is a compatibility shim, names the canonical import path (`context_graph.storage.backend.metadata`), and includes a deprecation notice
2. WHEN `context_graph/profile_vectors.py` exists as a Compatibility_Shim that re-exports storage types from `storage.backend.profile_vectors` and also defines service-layer classes, THE Refactoring_Process SHALL either (a) split the service-layer classes into a dedicated module under an appropriate sub-package and leave only re-exports in the shim, or (b) document the dual role clearly in the module docstring
3. THE Refactoring_Process SHALL add a `# COMPATIBILITY SHIM` comment at the top of each shim module for grep-ability
4. WHEN a shim module is identified, THE Top_Level_Init SHALL prefer importing from the canonical location rather than through the shim

### Requirement 5: Remove Dead References from the Top-Level __init__.py

**User Story:** As a developer, I want the package's public API to only reference modules that actually exist, so that import errors do not occur at runtime.

#### Acceptance Criteria

1. WHEN the Top_Level_Init references a module path that does not exist on disk, THE Refactoring_Process SHALL remove that entry from the `_EXPORTS` dictionary
2. THE Refactoring_Process SHALL remove or update the following dead references from the Top_Level_Init: entries pointing to `.episodes` (module does not exist), entries pointing to `.episode_processor` (module does not exist), entries pointing to `.legacy_graph_backend` (module does not exist), and entries pointing to `.graph_read` (module does not exist)
3. WHEN a dead reference is removed, THE Refactoring_Process SHALL also remove the corresponding entry from `__all__`
4. THE Top_Level_Init SHALL be updated to reflect the new module paths established by Requirements 1–3 (e.g., `.models` becomes `.models.graph`, `.query_parser` becomes `.query.parser`, `.embeddings` becomes `.runtime.embeddings`)

### Requirement 6: Remove Empty and Unused Packages and Directories

**User Story:** As a developer, I want dead code and empty placeholder packages removed, so that the codebase is honest about what is implemented.

#### Acceptance Criteria

1. WHEN `context_graph/storage/graph/` is an empty directory, THE Refactoring_Process SHALL remove the directory
2. WHEN `context_graph/server/__init__.py` is an empty file with no imports or definitions, THE Refactoring_Process SHALL remove the `context_graph/server/` directory entirely
3. WHEN `context_graph/storage/__init__.py` is an empty file, THE Refactoring_Process SHALL add a module-level docstring explaining the storage sub-package purpose (e.g., "Persistence backends and metadata storage for the context graph")
4. IF any removed directory is referenced by an import statement elsewhere in the codebase, THEN THE Refactoring_Process SHALL update or remove that import

### Requirement 7: Add Docstrings to All Sub-Package __init__.py Files

**User Story:** As a developer reading the codebase, I want every `__init__.py` to have a module-level docstring, so that I can understand each sub-package's purpose without reading all its files.

#### Acceptance Criteria

1. THE Package SHALL include a module-level docstring in every `__init__.py` file across all sub-packages (`code_identity`, `ingestion`, `memory`, `models`, `profiling`, `provenance`, `query`, `runtime`, `storage`, and their nested sub-packages including `runtime/embedding`, `runtime/llm`, `runtime/providers`, `ingestion/git`, `memory/prompts`, `provenance/prompts`, `storage/backend`)
2. WHEN a sub-package `__init__.py` already has a docstring, THE Refactoring_Process SHALL preserve the existing docstring
3. WHEN a sub-package `__init__.py` lacks a docstring, THE Refactoring_Process SHALL add a one-to-three sentence docstring describing the sub-package's responsibility

---

## Phase 2: Repository-Level Cleanup

---

### Requirement 8: Remove Non-Source Files from the Python Package

**User Story:** As a developer, I want the `context_graph/` package to contain only Python source code and `__init__.py` files, so that the package is clean and installable without non-code artifacts.

#### Acceptance Criteria

1. WHEN the Refactoring_Process is applied, THE Package SHALL contain only `.py` files and `__init__.py` modules (no `.md`, `.yml`, `.txt`, or `.local` files)
2. WHEN `context_graph/LOCAL_DEV.md` is found in the Package, THE Refactoring_Process SHALL remove the file since its content already redirects to `docs/local-dev.md`
3. WHEN `context_graph/PLAN.md` is found in the Package, THE Refactoring_Process SHALL move the file to `docs/plan.md`
4. WHEN `context_graph/PROVENANCE_ARCHITECTURE.md` is found in the Package, THE Refactoring_Process SHALL move the file to `docs/provenance-architecture.md`
5. WHEN `context_graph/docker-compose.local.yml` is found in the Package, THE Refactoring_Process SHALL delete the file since the root `docker-compose.yml` already serves the same purpose
6. WHEN `context_graph/requirements-local.txt` is found in the Package, THE Refactoring_Process SHALL delete the file since `pyproject.toml` is the canonical dependency source
7. WHEN sub-package README files exist (e.g. `context_graph/ingestion/README.md`, `context_graph/provenance/README.md`, `context_graph/storage/backend/README.md`), THE Refactoring_Process SHALL move each to a corresponding file under `docs/` (e.g. `docs/ingestion.md`, `docs/provenance.md`, `docs/storage-backend.md`) or merge content into existing docs

### Requirement 9: Restructure Tests to Mirror Source Layout

**User Story:** As a developer, I want the test directory structure to mirror the source package structure, so that I can find tests for any module quickly.

#### Acceptance Criteria

1. WHEN the Refactoring_Process is applied, THE Test_Suite SHALL organize test files into subdirectories that mirror the `context_graph/` package structure (e.g. `tests/ingestion/`, `tests/provenance/`, `tests/storage/`, `tests/query/`, `tests/models/`)
2. WHEN a test file covers a specific sub-package (e.g. `test_provenance.py` covers `context_graph/provenance/`), THE Refactoring_Process SHALL move the test file into the corresponding test subdirectory (e.g. `tests/provenance/test_provenance.py`)
3. THE Test_Suite SHALL include `conftest.py` files in each test subdirectory where shared fixtures are needed
4. WHEN the test restructuring is complete, THE CI_Pipeline SHALL continue to discover and run all tests without modification to the pytest command

### Requirement 10: Improve Test Coverage

**User Story:** As a course grader, I want to see meaningful test coverage across the core modules, so that I can assess the project's testing quality.

#### Acceptance Criteria

1. THE Test_Suite SHALL include unit tests for the `context_graph/ingestion/` sub-package covering snapshot building, diffing, and deterministic planning
2. THE Test_Suite SHALL include unit tests for the `context_graph/query/` sub-package covering code graph query construction and query parsing
3. THE Test_Suite SHALL include unit tests for the `context_graph/storage/backend/` sub-package covering the backend driver interface and factory
4. THE Test_Suite SHALL include unit tests for the `context_graph/code_identity/` sub-package covering canonicalization and resolution
5. THE Test_Suite SHALL include unit tests for the `context_graph/models/` sub-package covering data model serialization round-trips (to_dict/from_dict for CanonicalNode, CanonicalEdge, GraphSnapshot)
6. WHEN `pytest --cov=context_graph` is run, THE Test_Suite SHALL produce a coverage report that covers the ingestion, provenance, query, storage, models, and code_identity sub-packages

### Requirement 11: Enhance the README

**User Story:** As a course grader, I want the README to provide clear, complete instructions for setting up, running, testing, and deploying the project, so that I can reproduce the project from scratch.

#### Acceptance Criteria

1. THE Repository SHALL include a README.md at the root with sections for: project description, repo layout, prerequisites, quick start, running tests, running the evaluation/benchmark, deployment, and documentation links
2. THE README.md SHALL list all required prerequisites (Python version, uv, Docker, environment variables) with version constraints
3. THE README.md SHALL include step-by-step instructions for running the evaluation suite from `evaluation/`
4. THE README.md SHALL include a section describing the infrastructure-as-code setup under `infra/`
5. THE README.md SHALL include a section on how to run benchmarking code and reproduce results
6. WHEN a reader follows the README instructions on a clean machine with the listed prerequisites, THE Repository SHALL be runnable without undocumented manual steps

### Requirement 12: Add a CONTRIBUTING Guide

**User Story:** As a contributor or grader, I want a CONTRIBUTING.md that explains code style, testing conventions, and PR workflow, so that I understand the project's development practices.

#### Acceptance Criteria

1. THE Repository SHALL include a `CONTRIBUTING.md` file at the root
2. THE CONTRIBUTING.md SHALL describe the code style expectations (formatting, linting, type checking)
3. THE CONTRIBUTING.md SHALL describe how to run the test suite and what coverage expectations exist
4. THE CONTRIBUTING.md SHALL describe the branch and PR workflow used by the team

### Requirement 13: Add Linting and Type-Checking Configuration

**User Story:** As a developer, I want linting and type-checking configured in the project, so that code quality is enforced consistently.

#### Acceptance Criteria

1. THE Repository SHALL include a `ruff` configuration section in `pyproject.toml` for linting and formatting rules
2. THE Repository SHALL include a `mypy` or `pyright` configuration section in `pyproject.toml` for type checking
3. WHEN `ruff check context_graph/` is run, THE Package SHALL produce zero errors on the current codebase (or document known exceptions with inline `noqa` comments)
4. THE CI_Pipeline SHALL include a linting step that runs `ruff check` on pull requests

### Requirement 14: Make the Evaluation Suite Functional and Reproducible

**User Story:** As a course grader, I want the evaluation/benchmarking code to be runnable and produce reproducible results, so that I can assess the project's benchmarking quality.

#### Acceptance Criteria

1. THE Evaluation_Suite SHALL include a README.md that documents how to run the evaluation end-to-end
2. THE Evaluation_Suite SHALL include a runnable entry point that produces output to `evaluation/logs/`
3. WHEN `python -m evaluation.runners.eval_runner` is executed, THE Evaluation_Suite SHALL complete without errors and produce a `results.json` file
4. THE Evaluation_Suite SHALL document the dataset schema, metric definitions, and expected output format
5. IF the evaluation depends on external services (databases, APIs), THEN THE Evaluation_Suite SHALL document those dependencies and provide mock or stub alternatives for offline execution

### Requirement 15: Document Infrastructure-as-Code

**User Story:** As a course grader, I want the infrastructure code to be well-documented with clear setup and teardown instructions, so that I can assess the IaC quality.

#### Acceptance Criteria

1. THE Infra_Directory SHALL include a README.md that describes each Terraform module's purpose
2. THE Infra_Directory SHALL document the bootstrap process for initializing remote state
3. THE Infra_Directory SHALL document how to apply and destroy each environment stack (dev, prod)
4. THE Infra_Directory SHALL include a cost estimate or resource summary for the provisioned infrastructure
5. WHEN a reader follows the infra README instructions with valid AWS credentials, THE Infra_Directory SHALL allow provisioning the full stack without undocumented manual steps

### Requirement 16: Clean Up the .gitignore and Remove Tracked Artifacts

**User Story:** As a developer, I want the repository to not track generated artifacts or cache files, so that the repo stays clean.

#### Acceptance Criteria

1. THE Repository SHALL not track `__pycache__/` directories in version control
2. THE Repository SHALL not track `.pytest_cache/` directories in version control
3. THE .gitignore SHALL include entries for `__pycache__/`, `.pytest_cache/`, `*.pyc`, `.mypy_cache/`, `.ruff_cache/`, and `evaluation/logs/`
4. WHEN previously tracked cache directories exist, THE Refactoring_Process SHALL remove them from Git tracking using `git rm -r --cached`

### Requirement 17: Add Docstrings to Key Public Functions and Classes

**User Story:** As a developer or grader, I want key public APIs to have docstrings, so that the code is self-documenting.

#### Acceptance Criteria

1. THE Package SHALL include docstrings on all public classes exported from `context_graph/__init__.py`
2. THE Package SHALL include docstrings on all public methods of `IngestionDriver`, `QueryParser`, and backend driver classes
3. WHEN a public class or function lacks a docstring, THE Refactoring_Process SHALL add a concise docstring describing its purpose, parameters, and return value

### Requirement 18: Update the Top-Level __init__.py to Reflect New Package Layout

**User Story:** As a developer, I want the package's public API to use canonical import paths from the restructured layout, so that the lazy-loading `_EXPORTS` dictionary is accurate and maintainable.

#### Acceptance Criteria

1. WHEN Phase 1 restructuring is complete, THE Top_Level_Init SHALL update all `_EXPORTS` entries to reference the new canonical module paths (e.g., `.models.graph` instead of `.models`, `.query.parser` instead of `.query_parser`, `.runtime.embeddings` instead of `.embeddings`)
2. THE Top_Level_Init SHALL maintain backward compatibility so that `from context_graph import CanonicalNode` continues to work
3. THE Top_Level_Init SHALL not contain any entries pointing to modules that do not exist on disk
4. WHEN a new sub-package is created (e.g., `models/`), THE Top_Level_Init SHALL update `_EXPORTS` to import from the new sub-package `__init__.py`

### Requirement 19: Verify Import Compatibility After Restructuring

**User Story:** As a developer, I want confidence that the restructuring does not break existing import paths used by the MCP server or tests, so that the refactor is safe to merge.

#### Acceptance Criteria

1. WHEN the Refactoring_Process is complete, THE Test_Suite SHALL include an import smoke test that verifies all symbols listed in `context_graph/__init__.__all__` are importable
2. WHEN the Refactoring_Process is complete, THE Test_Suite SHALL include an import smoke test that verifies the MCP_App can import its required symbols from `context_graph`
3. IF a compatibility shim is provided for a moved module, THEN THE Test_Suite SHALL include a test that imports from both the old path and the new canonical path and verifies they resolve to the same object
