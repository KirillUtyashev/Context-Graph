## Vendored `codebase-memory-mcp` pipeline reference

This directory contains a small, reference-only subset of the upstream
`codebase-memory-mcp` project that is relevant to our indexing work.

Included:
- `src/pipeline/`
  - `pipeline.*`
  - `fqn.c`
  - `registry.c`
  - `confidence_band.*`
  - `pass_definitions.c`
  - `pass_calls.c`
  - `pass_usages.c`
  - `pass_semantic.c`
  - `pass_tests.c`
  - `pass_githistory.c`
  - `pass_gitdiff.c`
- `src/graph_buffer/`
- `src/discover/`
- `src/foundation/` (small dependency subset for the starter tests)
- `src/store/store.h`
- `internal/cbm/`
  - core extraction engine sources (`cbm.c`, `extract_*.c`, `helpers.*`, `lang_specs.*`)
  - lightweight runtime dependencies (`arena.*`, `ac.*`, `lz4_store.*`, `preprocessor.*`)
  - LSP helpers under `internal/cbm/lsp/`
  - tree-sitter runtime headers/sources under `internal/cbm/vendored/ts_runtime/`
  - vendored helper libraries under `internal/cbm/vendored/lz4/` and
    `internal/cbm/vendored/simplecpp/`
  - a narrowed grammar bundle for:
    - Python
    - C
    - C++
    - Java
    - JavaScript
    - TypeScript
    - Go
    - Bash
    - Rust
    - C#
- upstream license in `LICENSE.upstream`
- starter tests and local subset `Makefile`
- upstream extraction regression test in `tests/test_extraction.c`

Purpose:
- keep the indexing pipeline logic close at hand while we design our own version
- preserve the upstream structure/comments for porting and comparison
- avoid vendoring the full MCP server, SQLite store implementation, UI, and other
  product-specific pieces

Important:
- this is not a standalone, buildable import of the upstream project
- several dependencies are intentionally omitted because we are using this as a
  porting/reference slice, not as a drop-in third-party library
- in particular, the upstream vendored grammar bundle is intentionally omitted;
  that bundle is roughly 649 MB on disk and would dominate the scope of this
  repo copy
- if we reuse upstream code beyond reference/porting, preserve attribution and
  license requirements from `LICENSE.upstream`

Starter workflow:
- run `make test` in this directory
- this currently builds a small subset around:
  - language detection
  - gitignore matching
  - repo discovery
  - FQN / registry helpers
- run `make test-extraction` to build the narrowed extraction slice
- unsupported languages are stubbed intentionally so the upstream
  `lang_specs.c` can stay close to source without requiring the full grammar
  corpus
