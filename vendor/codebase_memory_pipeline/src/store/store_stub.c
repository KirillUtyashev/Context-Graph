#include "store/store.h"

struct cbm_store {};

int cbm_store_upsert_project(cbm_store_t *s, const char *name, const char *root_path) {
    (void)s;
    (void)name;
    (void)root_path;
    return CBM_STORE_OK;
}

int cbm_store_begin_bulk(cbm_store_t *s) {
    (void)s;
    return CBM_STORE_OK;
}

int cbm_store_drop_indexes(cbm_store_t *s) {
    (void)s;
    return CBM_STORE_OK;
}

int cbm_store_begin(cbm_store_t *s) {
    (void)s;
    return CBM_STORE_OK;
}

int cbm_store_delete_edges_by_project(cbm_store_t *s, const char *project) {
    (void)s;
    (void)project;
    return CBM_STORE_OK;
}

int cbm_store_delete_nodes_by_project(cbm_store_t *s, const char *project) {
    (void)s;
    (void)project;
    return CBM_STORE_OK;
}

int64_t cbm_store_upsert_node(cbm_store_t *s, const cbm_node_t *n) {
    (void)s;
    (void)n;
    return 1;
}

int64_t cbm_store_insert_edge(cbm_store_t *s, const cbm_edge_t *e) {
    (void)s;
    (void)e;
    return 1;
}

int cbm_store_commit(cbm_store_t *s) {
    (void)s;
    return CBM_STORE_OK;
}

int cbm_store_create_indexes(cbm_store_t *s) {
    (void)s;
    return CBM_STORE_OK;
}

int cbm_store_end_bulk(cbm_store_t *s) {
    (void)s;
    return CBM_STORE_OK;
}
