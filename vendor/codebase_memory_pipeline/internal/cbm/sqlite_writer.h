#ifndef CBM_SQLITE_WRITER_H
#define CBM_SQLITE_WRITER_H

#include <stdint.h>

typedef struct {
    int64_t id;
    const char *project;
    const char *label;
    const char *name;
    const char *qualified_name;
    const char *file_path;
    int start_line;
    int end_line;
    const char *properties;
} CBMDumpNode;

typedef struct {
    int64_t id;
    const char *project;
    int64_t source_id;
    int64_t target_id;
    const char *type;
    const char *properties;
    const char *url_path;
} CBMDumpEdge;

int cbm_write_db(const char *path, const char *project, const char *root_path,
                 const char *indexed_at, const CBMDumpNode *nodes, int node_count,
                 const CBMDumpEdge *edges, int edge_count);

#endif /* CBM_SQLITE_WRITER_H */
