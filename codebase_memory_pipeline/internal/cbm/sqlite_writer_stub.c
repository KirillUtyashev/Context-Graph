#include "sqlite_writer.h"

int cbm_write_db(const char *path, const char *project, const char *root_path,
                 const char *indexed_at, const CBMDumpNode *nodes, int node_count,
                 const CBMDumpEdge *edges, int edge_count) {
    (void)path;
    (void)project;
    (void)root_path;
    (void)indexed_at;
    (void)nodes;
    (void)node_count;
    (void)edges;
    (void)edge_count;
    return -1;
}
