from unittest.mock import Mock

import pytest

from context_graph.backend import InMemoryMetadataStore
from context_graph.backend.profile_vector_services import ProfileSearchService
from context_graph.backend.profile_vectors import ProfileVectorHit
from context_graph.models.profiles import SEMANTIC_CLAIM_PROFILE_KIND, ProfileRecord
from context_graph.provenance.claim_retrieval import ClaimSemanticRetrievalService
from context_graph.query.parser import QueryParseResult


def _profile(index: int) -> ProfileRecord:
    return ProfileRecord(
        profile_id=f"profile:{index}",
        repo_id="demo",
        profile_kind=SEMANTIC_CLAIM_PROFILE_KIND,
        profile_target_kind="Claim",
        profile_target_id=f"claim:{index}",
        relation_type=None,
        source_node_id=None,
        source_node_kind=None,
        target_node_id=None,
        target_node_kind=None,
        target_revision_id=None,
        value_text=f"Claim text {index}",
        keys=(),
        metadata={
            "claim_id": f"claim:{index}",
            "claim_text": f"Claim text {index}",
            "claim_kind": "change_claim",
        },
    )


@pytest.mark.parametrize("service_type", [ClaimSemanticRetrievalService, ProfileSearchService])
@pytest.mark.parametrize("missing_indices", [(), (0,), (1,), (2,), (0, 1, 2)])
def test_search_matches_hits_to_profiles_by_id(service_type, missing_indices) -> None:
    profiles = tuple(_profile(index) for index in range(3))
    metadata = InMemoryMetadataStore()
    # Load metadata in a different order from the vector ranking.
    metadata.upsert_profiles(
        tuple(profiles[index] for index in reversed(range(3)) if index not in missing_indices)
    )
    hits = tuple(
        ProfileVectorHit(
            profile_id=profile.profile_id,
            profile_key_id=f"key:{index}",
            repo_id="demo",
            profile_kind=SEMANTIC_CLAIM_PROFILE_KIND,
            key_text=f"Evidence key {index}",
            key_origin="raw_text",
            score=1.0 - index / 10,
        )
        for index, profile in enumerate(profiles)
    )
    service = service_type(
        metadata_store=metadata,
        vector_store=Mock(search_profile_keys=Mock(return_value=tuple(reversed(hits)))),
        embedding_provider=Mock(embed_texts=Mock(return_value=[[1.0]])),
        query_parser=Mock(parse=Mock(return_value=QueryParseResult("query", (), (), ()))),
    )

    result = service.search(repo_id="demo", query="query", limit=3, llm_enabled=False)

    expected_indices = tuple(index for index in range(3) if index not in missing_indices)
    assert result.profiles == tuple(profiles[index] for index in expected_indices)
    assert tuple(hit.profile_id for hit in result.hits) == tuple(
        profiles[index].profile_id for index in expected_indices
    )
    for hit, index in zip(result.hits, expected_indices):
        assert hit.score == hits[index].score
        assert hit.key_text == hits[index].key_text
        assert hit.key_origin == hits[index].key_origin
        if service_type is ClaimSemanticRetrievalService:
            assert hit.claim_id == f"claim:{index}"
            assert hit.claim_text == f"Claim text {index}"
            assert hit.claim_kind == "change_claim"
