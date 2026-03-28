from __future__ import annotations

from context_graph.entity_nodes import (
    EntityNode,
    entity_node_aliases,
    entity_node_file_path,
    entity_node_kind,
    entity_node_qualified_name,
    entity_node_searchable_terms,
    make_entity_node,
)


def test_make_entity_node_populates_labels_and_attributes():
    node = make_entity_node(
        node_id="node-1",
        repo_id="repo",
        kind="Function",
        name="run",
        qualified_name="pkg.module.run",
        file_path="src/pkg/module.py",
        aliases=("run", "execute"),
        metadata={"lang": "py"},
    )

    assert node == EntityNode(
        uuid="node-1",
        name="run",
        group_id="repo",
        labels=("Function",),
        attributes={
            "qualified_name": "pkg.module.run",
            "file_path": "src/pkg/module.py",
            "aliases": ["run", "execute"],
            "lang": "py",
        },
    )


def test_make_entity_node_uses_empty_labels_for_generic_entity():
    node = make_entity_node(
        node_id="node-1",
        repo_id="repo",
        kind="Entity",
        name="thing",
    )

    assert node.labels == ()
    assert entity_node_kind(node) == "Entity"


def test_entity_node_accessors_handle_missing_and_mixed_values():
    node = EntityNode(
        uuid="node-1",
        name="Thing",
        group_id="repo",
        labels=("Entity", "Class"),
        attributes={
            "qualified_name": " pkg.Thing ",
            "file_path": " src/pkg.py ",
            "aliases": [" Alias ", "", 123],
        },
    )

    assert entity_node_kind(node) == "Class"
    assert entity_node_qualified_name(node) == "pkg.Thing"
    assert entity_node_file_path(node) == "src/pkg.py"
    assert entity_node_aliases(node) == (" Alias ", "123")


def test_entity_node_searchable_terms_include_basename_and_deduplicate():
    node = EntityNode(
        uuid="node-1",
        name="run",
        group_id="repo",
        labels=("Function",),
        attributes={
            "qualified_name": "pkg.module.run",
            "file_path": "src/pkg/module.py",
            "aliases": ["run", "execute"],
        },
    )

    assert entity_node_searchable_terms(node) == (
        "execute",
        "module.py",
        "pkg.module.run",
        "run",
        "src/pkg/module.py",
    )

