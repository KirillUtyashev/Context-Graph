# Requirements Document

## Introduction

Phase 2 cleanup of the `context_graph` repository. This phase addresses four scoped items: removing non-source files from the Python package, cleaning up tracked `__pycache__/` directories, adding a basic `ruff` linting configuration, and restructuring the flat `tests/` directory to mirror the source layout.

Phase 1 (completed in `.kiro/specs/repo-refactor/`) handled internal package restructuring — consolidating models, relocating misplaced modules, cleaning dead references, and updating `__init__.py` exports. This phase focuses on repository-level hygiene.

## Glossary

- **Package**: The `context_graph/` Python package containing library source code
- **Repository**: The Context Graph Git repository and its file tree
- **Test_Suite**: The `tests/` directory containing pytest test files for the core library
- **Docs_Directory**: The `docs/` directory containing project documentation
- **Refactoring_Process**: The set of file moves, deletions, and git operations applied to the repository
- **Stale_Cache**: A `__pycache__/` directory or `.pyc` file tracked by Git despite being in `.gitignore`

## Requirements

---

### Requirement 1: Remove Non-Source Files from the Python Package

**User Story:** As a developer, I want the `context_graph/` package to contain only Python source code, so that the package is clean, installable, and free of documentation artifacts that belong elsewhere.

#### Acceptance Criteria

1. WHEN `context_graph/LOCAL_DEV.md` is found in the Package, THE Refactoring_Process SHALL delete the file, since its content already redirects to `docs/local-dev.md` and contains no unique information
2. WHEN `context_graph/PLAN.md` is found in the Package, THE Refactoring_Process SHALL move the file to `docs/plan.md` to preserve the project vision document outside the installable package
3. WHEN `context_graph/backend/README.md` is found in the Package, THE Refactoring_Process SHALL move the file to `docs/backend.md` to preserve the backend interface documentation outside the installable package
4. WHEN `context_graph/ingestion/README.md` is found in the Package, THE Refactoring_Process SHALL evaluate whether its content duplicates `docs/ingestion.md`; IF the content is redundant, THEN THE Refactoring_Process SHALL delete the file; IF the content adds unique detail, THEN THE Refactoring_Process SHALL move it to `docs/ingestion-internals.md`
5. WHEN `context_graph/provenance/README.md` is found in the Package, THE Refactoring_Process SHALL move the file to `docs/provenance.md` to preserve the provenance pipeline documentation outside the installable package
6. WHEN `context_graph/backend/__init__ 2.py` is found in the Package, THE Refactoring_Process SHALL delete the file, since it is a stale copy artifact (note the space in the filename)
7. WHEN the Refactoring_Process is complete, THE Package SHALL contain only `.py` files (no `.md`, `.yml`, `.txt`, or other non-source files)

---

### Requirement 2: Clean Up Tracked __pycache__ Directories

**User Story:** As a developer, I want all `__pycache__/` directories and `.pyc` files removed from Git tracking, so that the repository does not contain stale compiled bytecode for modules that may no longer exist.

#### Acceptance Criteria

1. THE Repository `.gitignore` SHALL already contain an entry for `__pycache__/` (verify, do not duplicate)
2. WHEN tracked `__pycache__/` directories exist under `context_graph/`, THE Refactoring_Process SHALL remove them from Git tracking using `git rm -r --cached` for each tracked `__pycache__/` directory
3. WHEN tracked `__pycache__/` directories exist at the repository root or under `tests/`, THE Refactoring_Process SHALL remove them from Git tracking using `git rm -r --cached`
4. WHEN the cleanup is complete, THE Repository SHALL have zero `__pycache__/` directories or `.pyc` files in the Git index
5. THE Refactoring_Process SHALL preserve the `.gitignore` entries for `__pycache__/` and `*.py[cod]` so that future bytecode is never tracked

---

### Requirement 3: Add Linting Configuration

**User Story:** As a developer, I want a basic `ruff` configuration in `pyproject.toml`, so that the team has consistent formatting and linting rules enforced from a single config source.

#### Acceptance Criteria

1. THE Repository `pyproject.toml` SHALL include a `[tool.ruff]` section specifying the target Python version consistent with the project's `requires-python` constraint (3.10+)
2. THE Repository `pyproject.toml` SHALL include a `[tool.ruff.lint]` section with a curated set of enabled rule prefixes (at minimum: `E` for pycodestyle errors, `F` for pyflakes, `I` for isort)
3. THE Repository `pyproject.toml` SHALL include a `[tool.ruff.lint.per-file-ignores]` section to suppress rules on test files where appropriate (e.g., allowing unused imports in `conftest.py` or long lines in test assertions)
4. THE `[tool.ruff]` section SHALL set a `line-length` value consistent with the project's existing code style
5. WHEN `ruff` is added to the dev dependency group in `pyproject.toml`, THE Refactoring_Process SHALL add it with a version constraint

---

### Requirement 4: Restructure Tests to Mirror Source Layout

**User Story:** As a developer, I want the test directory structure to mirror the `context_graph/` package structure, so that I can find and run targeted tests for any sub-package quickly.

#### Acceptance Criteria

1. WHEN the Refactoring_Process is applied, THE Test_Suite SHALL organize test files into subdirectories that mirror the `context_graph/` package structure (e.g., `tests/backend/`, `tests/provenance/`, `tests/profiling/`, `tests/runtime/`)
2. WHEN `tests/test_backend.py` covers `context_graph/backend/` and `context_graph/ingestion/`, THE Refactoring_Process SHALL move the file to `tests/backend/test_backend.py`
3. WHEN `tests/test_provenance.py` covers `context_graph/provenance/`, THE Refactoring_Process SHALL move the file to `tests/provenance/test_provenance.py`
4. WHEN `tests/test_embeddings_inference.py` covers `context_graph/runtime/` embedding settings, THE Refactoring_Process SHALL move the file to `tests/runtime/test_embeddings_inference.py`
5. WHEN `tests/test_selected_function_profiling.py` covers `context_graph/profiling/`, THE Refactoring_Process SHALL move the file to `tests/profiling/test_selected_function_profiling.py`
6. WHEN `tests/test_small_modules.py` covers multiple sub-packages (runtime, query, CLI), THE Refactoring_Process SHALL move the file to `tests/test_small_modules.py` (keep at root since it is cross-cutting) or split it into sub-package-specific files
7. THE Test_Suite SHALL include an `__init__.py` file in each new test subdirectory to ensure pytest discovery works correctly
8. THE Test_Suite SHALL preserve the root `tests/conftest.py` for shared fixtures; IF subdirectory-specific fixtures are needed, THEN THE Refactoring_Process SHALL create `conftest.py` files in those subdirectories
9. WHEN the test restructuring is complete, THE Test_Suite SHALL pass all existing tests without modification to the pytest command (`pytest tests/`)
10. WHEN the test restructuring is complete, THE Test_Suite SHALL support targeted test runs per sub-package (e.g., `pytest tests/provenance/` runs only provenance tests)
