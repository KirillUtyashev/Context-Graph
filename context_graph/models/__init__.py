"""Core data model definitions for the context graph.

This sub-package consolidates graph snapshot models, entity node helpers,
and profile record types into a single location.
"""

from .graph import (
    CanonicalEdge,
    CanonicalNode,
    EdgeChange,
    GraphSnapshot,
    JsonDict,
    NodeChange,
    SnapshotApplyResult,
    SnapshotDiff,
    canonical_json,
    utc_now_iso,
)
from .entity_nodes import (
    EntityNode,
    entity_node_aliases,
    entity_node_file_path,
    entity_node_kind,
    entity_node_qualified_name,
    entity_node_searchable_terms,
    make_entity_node,
)
from .profiles import (
    CLAIM_TARGET_KIND,
    FACT_EDGE_TARGET_KIND,
    OBSERVATION_TARGET_KIND,
    PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC,
    PROFILE_KEY_ORIGIN_LEGACY_KEYWORD,
    PROFILE_KEY_ORIGIN_RAW_TEXT,
    ProfileDeactivation,
    ProfileKeyDefinition,
    ProfileRecord,
    RelationProfileSpec,
    SEMANTIC_CLAIM_PROFILE_KIND,
    SEMANTIC_FACT_PROFILE_KIND,
    SEMANTIC_OBSERVATION_PROFILE_KIND,
    SEMANTIC_RELATION_PROFILE_KIND,
    observation_profile_text,
    profile_deactivation_from_fact_invalidation,
    semantic_claim_profile_from_claim,
    semantic_fact_profile_from_fact,
    semantic_observation_profile_from_observation,
    semantic_relation_profile_from_spec,
)
