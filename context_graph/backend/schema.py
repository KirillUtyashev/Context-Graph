"""Internal storage conventions used by the additive backend.

V1 keeps lexical indexing generic by storing dedicated search documents rather
than indexing arbitrary graph labels or relationship types directly. Search
documents point back to the real graph record via `record_id`, then `fetch()`
hydrates the original graph record on demand.
"""

SEARCH_DOCUMENT_TEXT_PROPERTY = "search_text"
SEARCH_DOCUMENT_ID_PROPERTY = "record_id"
SEARCH_DOCUMENT_TYPE_PROPERTY = "record_type"
SEARCH_DOCUMENT_REPO_ID_PROPERTY = "repo_id"
SEARCH_DOCUMENT_GROUP_ID_PROPERTY = "group_id"

GRAPH_RECORD_ID_PROPERTY = "record_id"
GRAPH_RECORD_TYPE_PROPERTY = "record_type"
GRAPH_REPO_ID_PROPERTY = "repo_id"
GRAPH_GROUP_ID_PROPERTY = "group_id"

NODE_SEARCH_DOCUMENT_LABEL = "NodeSearchDocument"
EDGE_SEARCH_DOCUMENT_LABEL = "EdgeSearchDocument"

NEO4J_NODE_SEARCH_INDEX = "backend_node_search_documents"
NEO4J_EDGE_SEARCH_INDEX = "backend_edge_search_documents"
NEO4J_GRAPH_NODE_RECORD_ID_INDEX = "backend_graph_node_record_id"
NEO4J_GRAPH_EDGE_RECORD_ID_INDEX = "backend_graph_edge_record_id"

NEPTUNE_NODE_AOSS_INDEX = "backend_node_documents"
NEPTUNE_EDGE_AOSS_INDEX = "backend_edge_documents"
