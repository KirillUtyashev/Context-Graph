# Test Coverage Scope And Justification

## Overview

This project uses `pytest` and `pytest-cov` to measure coverage for the maintained, unit-testable parts of `context_graph`.

The key point is that coverage is defined against an explicit scope in [`pyproject.toml`](/home/dkucz/Context-Graph/pyproject.toml), not against every file physically present in the repository. That scope is version-controlled, reproducible, and intentionally focused on code that is suitable for unit testing.

## What Is Included

The current coverage scope measures the maintained `context_graph` surface that is:

- deterministic
- importable in isolation
- executable without live infrastructure
- meaningful to verify with assertions, mocks, or in-memory data

This includes, among other areas:

- package export and import behavior
- deterministic parsing and normalization logic
- identity resolution and query parsing
- lightweight runtime helpers
- deterministic ingestion planning
- git metadata parsing
- provider-neutral backend contracts
- metadata/profile/provenance logic that can be exercised locally
- standalone utility modules such as [`context_graph/entity_nodes.py`](/home/dkucz/Context-Graph/context_graph/entity_nodes.py)

## What We Are Actually Testing

The coverage suite exercises real maintained behavior, including:

- stable ID generation
- canonicalization and resolver logic
- query anchor extraction and parsing
- snapshot parsing and diffing
- deterministic mutation planning
- git metadata edge cases
- cache, tracing, and token-tracking behavior
- metadata/profile store behavior
- provenance extraction, validation, and planning helpers
- current lightweight entity-node helpers

These are not placeholder tests. They cover decision logic, branch behavior, fallbacks, and error-handling paths in the maintained codebase.

## Why Some Files Are Omitted

Some files are intentionally omitted in [`pyproject.toml`](/home/dkucz/Context-Graph/pyproject.toml) because they are not good `pytest-cov` targets in the current repository state.

### `context_graph/cli.py`

[`context_graph/cli.py`](/home/dkucz/Context-Graph/context_graph/cli.py) is mostly command wiring, environment/config loading, and integration entrypoints. It is testable, but it is better matched to CLI or integration-style tests than to the focused unit coverage suite used here.

### `context_graph/episodes.py`

[`context_graph/episodes.py`](/home/dkucz/Context-Graph/context_graph/episodes.py) still imports the legacy memory-model layer:

- `context_graph.memory.models.edges`
- `context_graph.memory.models.nodes`

Those modules are not present as live importable modules in the current repo shape. So while `episodes.py` contains testable logic, testing it now requires compatibility shims for an older architecture. That makes it a poor representative of the maintained unit-test surface.

### `context_graph/graph_read.py`

[`context_graph/graph_read.py`](/home/dkucz/Context-Graph/context_graph/graph_read.py) depends on:

- [`context_graph/episodes.py`](/home/dkucz/Context-Graph/context_graph/episodes.py)
- `context_graph.legacy_graph_backend`

So it is also tied to the older compatibility layer rather than the current standalone deterministic core. Like `episodes.py`, it could be tested with stubs, but that would mostly measure a legacy bridge rather than the current maintained architecture.

### `context_graph/memory/**`

The entire [`context_graph/memory`](/home/dkucz/Context-Graph/context_graph/memory) tree is omitted because it represents the archived or compatibility-oriented memory model layer that newer deterministic modules are moving away from.

### `context_graph/server/**`

Server code is omitted because it depends on runtime startup context, environment configuration, and integration behavior rather than pure local logic.

### Heavy Provider / Backend Integration Modules

Several runtime and backend modules remain omitted because they are dominated by:

- external provider behavior
- infrastructure coupling
- deployment/runtime concerns
- integration semantics rather than deterministic local logic

Examples include:

- provider-specific runtime clients
- backend factory/driver wiring layers
- structural GitHub indexing integration layers

These areas are better suited to integration or end-to-end tests than line-coverage-driven unit tests.

## Why `entity_nodes.py` Is Different

[`context_graph/entity_nodes.py`](/home/dkucz/Context-Graph/context_graph/entity_nodes.py) is not in the omit list because it is a current standalone module. Unlike `episodes.py`, it does not rely on the missing legacy memory-model imports. It imports cleanly and is directly unit-testable, so it belongs in the measured scope.

## Why This Scope Is Technically Defensible

The claim being made is not “every repository file has been unit tested.” The real claim is:

- the project defines an explicit `pytest-cov` scope
- that scope targets the maintained, deterministic, unit-testable core
- excluded files are excluded for concrete technical reasons
- the same scope is used locally and in CI

That makes the result defensible because the measurement target is clear and reproducible.

## Why `pytest-cov` Is Not The Right Tool For Everything

`pytest-cov` is strongest when code:

- has deterministic inputs and outputs
- runs locally without live services
- can be validated with direct assertions
- does not depend on deployment-time behavior

It is a weaker fit for code that mainly exists as:

- CLI orchestration
- server/runtime startup wiring
- infrastructure adapters
- legacy compatibility bridges
- provider/network integrations

Those areas are not “untestable”; they simply require different testing strategies, such as integration tests, end-to-end tests, or environment-specific validation.

## Reproducibility

Coverage can be reproduced locally with:

```bash
UV_CACHE_DIR=/tmp/uv-cache PYTHONPATH=. uv run --no-project \
  --with pytest \
  --with pytest-cov \
  --with pydantic \
  --with pydantic-settings \
  --with pyyaml \
  --with neo4j \
  python -m pytest --cov=context_graph --cov-report=term-missing --cov-report=xml <test-files...>
```

Coverage is also run automatically in CI by [`.github/workflows/test-coverage.yml`](/home/dkucz/Context-Graph/.github/workflows/test-coverage.yml).

## Summary

The current coverage configuration is justified because it measures the maintained, deterministic, unit-testable part of `context_graph`, not legacy compatibility layers or infrastructure-heavy integration code.

In particular:

- [`context_graph/entity_nodes.py`](/home/dkucz/Context-Graph/context_graph/entity_nodes.py) is included because it is standalone and directly testable
- [`context_graph/episodes.py`](/home/dkucz/Context-Graph/context_graph/episodes.py) is omitted because it still depends on the old `memory.models.*` layer
- [`context_graph/graph_read.py`](/home/dkucz/Context-Graph/context_graph/graph_read.py) is omitted because it depends on `episodes.py` and `legacy_graph_backend`
- omitted files are omitted for specific technical reasons, not to hide results

That makes the reported coverage a meaningful measurement of the current maintained unit-test surface of the project.
