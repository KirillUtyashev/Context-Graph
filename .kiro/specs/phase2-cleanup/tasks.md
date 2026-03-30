# Implementation Plan: Phase 2 Cleanup

## Overview

Repository-hygiene tasks for the `context_graph` project: remove non-source files from the package, clean tracked `__pycache__/` from Git, add ruff linting config, and restructure tests to mirror source layout. All changes are file-level operations, git commands, and config edits — no runtime code is modified.

## Tasks

- [x] 1. Remove non-source files from context_graph/ package
  - [x] 1.1 Delete `context_graph/LOCAL_DEV.md` (redirect stub, no unique content)
    - _Requirements: 1.1_
  - [x] 1.2 Move `context_graph/PLAN.md` to `docs/plan.md`
    - _Requirements: 1.2_
  - [x] 1.3 Move `context_graph/backend/README.md` to `docs/backend.md`
    - _Requirements: 1.3_
  - [x] 1.4 Move `context_graph/ingestion/README.md` to `docs/ingestion-internals.md`
    - The design determined this content adds unique detail beyond `docs/ingestion.md`
    - _Requirements: 1.4_
  - [x] 1.5 Move `context_graph/provenance/README.md` to `docs/provenance.md`
    - _Requirements: 1.5_
  - [x] 1.6 Delete `context_graph/backend/__init__ 2.py` (stale copy artifact with space in filename)
    - _Requirements: 1.6_
  - [ ]* 1.7 Write property test for package purity (Property 1)
    - **Property 1: Package purity** — Walk all files under `context_graph/` and assert every file has a `.py` extension
    - **Validates: Requirements 1.7**

- [x] 2. Clean up tracked __pycache__/ directories from Git
  - [x] 2.1 Verify `.gitignore` already contains `__pycache__/` and `*.py[cod]` entries
    - Do not duplicate entries; only verify they exist
    - _Requirements: 2.1, 2.5_
  - [x] 2.2 Run `git rm -r --cached` on all tracked `__pycache__/` directories
    - Use `find . -type d -name __pycache__ -exec git rm -r --cached {} + 2>/dev/null` or equivalent
    - Covers repo root, `tests/`, and all `context_graph/` subdirectories
    - _Requirements: 2.2, 2.3_
  - [ ]* 2.3 Write property test for git index cleanliness (Property 2)
    - **Property 2: Git index cleanliness** — List all paths in the git index and assert none contain `__pycache__` or end with `.pyc`/`.pyo`/`.pyd`
    - **Validates: Requirements 2.2, 2.3, 2.4**

- [x] 3. Checkpoint — Verify file cleanup
  - Ensure non-source files are removed/moved, `__pycache__/` directories are untracked, and all property tests pass. Ask the user if questions arise.

- [x] 4. Add ruff linting configuration to pyproject.toml
  - [x] 4.1 Add `[tool.ruff]` section with `target-version = "py310"` and `line-length = 120`
    - _Requirements: 3.1, 3.4_
  - [x] 4.2 Add `[tool.ruff.lint]` section with `select = ["E", "F", "I"]`
    - _Requirements: 3.2_
  - [x] 4.3 Add `[tool.ruff.lint.per-file-ignores]` section
    - Suppress `E501` in `tests/**` and `F401` in `tests/**/conftest.py`
    - _Requirements: 3.3_
  - [x] 4.4 Add `ruff>=0.11,<1` to the `[dependency-groups] dev` list
    - _Requirements: 3.5_

- [x] 5. Restructure tests/ to mirror source layout
  - [x] 5.1 Create test subdirectories with `__init__.py` files
    - Create `tests/backend/__init__.py`, `tests/provenance/__init__.py`, `tests/runtime/__init__.py`, `tests/profiling/__init__.py`
    - _Requirements: 4.1, 4.7_
  - [x] 5.2 Move `tests/test_backend.py` to `tests/backend/test_backend.py`
    - _Requirements: 4.2_
  - [x] 5.3 Move `tests/test_provenance.py` to `tests/provenance/test_provenance.py`
    - _Requirements: 4.3_
  - [x] 5.4 Move `tests/test_embeddings_inference.py` to `tests/runtime/test_embeddings_inference.py`
    - _Requirements: 4.4_
  - [x] 5.5 Move `tests/test_selected_function_profiling.py` to `tests/profiling/test_selected_function_profiling.py`
    - _Requirements: 4.5_
  - [x] 5.6 Keep `tests/test_small_modules.py` and `tests/conftest.py` at root
    - Verify both files remain at `tests/` root (cross-cutting tests and shared fixtures)
    - _Requirements: 4.6, 4.8_
  - [ ]* 5.7 Write property test for test subdirectory discovery (Property 3)
    - **Property 3: Test subdirectory discovery** — For each subdirectory of `tests/` containing `test_*.py` files, assert `__init__.py` exists
    - **Validates: Requirements 4.7**

- [x] 6. Final checkpoint — Verify all changes
  - Ensure all tests pass with `pytest tests/`, targeted runs work (e.g., `pytest tests/provenance/`), and `pyproject.toml` parses correctly. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Property tests validate universal correctness properties from the design
- No runtime code is modified — only file locations, git index, and config
