#include "discover/discover.h"
#include "graph_buffer/graph_buffer.h"
#include "pipeline/pipeline.h"
#include "pipeline/pipeline_internal.h"
#include "foundation/hash_table.h"
#include "cbm.h"

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void free_seen_dir_key(const char *key, void *val, void *ud) {
    (void)val;
    (void)ud;
    free((void *)key);
}

static int pass_structure(cbm_gbuf_t *gbuf, const char *project_name, const cbm_file_info_t *files,
                          int file_count) {
    cbm_gbuf_upsert_node(gbuf, "Project", project_name, project_name, NULL, 0, 0, "{}");

    CBMHashTable *seen_dirs = cbm_ht_create(256);
    if (!seen_dirs) {
        return -1;
    }

    for (int i = 0; i < file_count; i++) {
        const char *rel = files[i].rel_path;
        if (!rel) {
            continue;
        }

        char *file_qn = cbm_pipeline_fqn_compute(project_name, rel, "__file__");
        const char *slash = strrchr(rel, '/');
        const char *basename = slash ? slash + 1 : rel;

        char props[256];
        const char *ext = strrchr(basename, '.');
        snprintf(props, sizeof(props), "{\"extension\":\"%s\"}", ext ? ext : "");

        cbm_gbuf_upsert_node(gbuf, "File", basename, file_qn, rel, 0, 0, props);

        char *dir = strdup(rel);
        char *last_slash = strrchr(dir, '/');
        if (last_slash) {
            *last_slash = '\0';
        } else {
            free(dir);
            dir = strdup("");
        }

        const char *parent_qn = project_name;
        char *parent_qn_heap = NULL;
        if (dir[0] != '\0') {
            parent_qn_heap = cbm_pipeline_fqn_folder(project_name, dir);
            parent_qn = parent_qn_heap;
        }

        char *walk_dir = strdup(dir);
        while (walk_dir[0] != '\0' && !cbm_ht_get(seen_dirs, walk_dir)) {
            cbm_ht_set(seen_dirs, strdup(walk_dir), (void *)1);

            char *folder_qn = cbm_pipeline_fqn_folder(project_name, walk_dir);
            const char *dir_base = strrchr(walk_dir, '/');
            dir_base = dir_base ? dir_base + 1 : walk_dir;

            cbm_gbuf_upsert_node(gbuf, "Folder", dir_base, folder_qn, walk_dir, 0, 0, "{}");

            char *pdir = strdup(walk_dir);
            char *ps = strrchr(pdir, '/');
            if (ps) {
                *ps = '\0';
            } else {
                free(pdir);
                pdir = strdup("");
            }

            const char *pqn = project_name;
            char *pqn_heap = NULL;
            if (pdir[0] != '\0') {
                pqn_heap = cbm_pipeline_fqn_folder(project_name, pdir);
                pqn = pqn_heap;
            }

            const cbm_gbuf_node_t *fn = cbm_gbuf_find_by_qn(gbuf, folder_qn);
            const cbm_gbuf_node_t *pn = cbm_gbuf_find_by_qn(gbuf, pqn);
            if (fn && pn) {
                cbm_gbuf_insert_edge(gbuf, pn->id, fn->id, "CONTAINS_FOLDER", "{}");
            }

            free(folder_qn);
            free(pqn_heap);

            char *up = strrchr(walk_dir, '/');
            if (up) {
                *up = '\0';
            } else {
                walk_dir[0] = '\0';
            }
            free(pdir);
        }

        const cbm_gbuf_node_t *file_node = cbm_gbuf_find_by_qn(gbuf, file_qn);
        const cbm_gbuf_node_t *parent_node = cbm_gbuf_find_by_qn(gbuf, parent_qn);
        if (file_node && parent_node) {
            cbm_gbuf_insert_edge(gbuf, parent_node->id, file_node->id, "CONTAINS_FILE", "{}");
        }

        free(file_qn);
        free(dir);
        free(walk_dir);
        free(parent_qn_heap);
    }

    cbm_ht_foreach(seen_dirs, free_seen_dir_key, NULL);
    cbm_ht_free(seen_dirs);
    return 0;
}

static void free_result_cache(CBMFileResult **result_cache, int file_count) {
    if (!result_cache) {
        return;
    }
    for (int i = 0; i < file_count; i++) {
        if (result_cache[i]) {
            cbm_free_result(result_cache[i]);
        }
    }
    free(result_cache);
}

int main(int argc, char **argv) {
    if (argc != 3 && argc != 4) {
        fprintf(stderr, "usage: %s <repo-path> <output-json> [project-name]\n", argv[0]);
        return 2;
    }

    const char *repo_path = argv[1];
    const char *output_path = argv[2];
    const char *project_name_arg = (argc == 4) ? argv[3] : NULL;

    cbm_discover_opts_t opts = {0};
    opts.mode = CBM_MODE_FULL;

    cbm_file_info_t *files = NULL;
    int file_count = 0;
    if (cbm_discover(repo_path, &opts, &files, &file_count) != 0) {
        fprintf(stderr, "failed to discover files under %s\n", repo_path);
        return 1;
    }

    char *project_name =
        project_name_arg ? strdup(project_name_arg) : cbm_project_name_from_path(repo_path);
    cbm_gbuf_t *gbuf = cbm_gbuf_new(project_name, repo_path);
    cbm_registry_t *registry = cbm_registry_new();
    atomic_int cancelled;
    atomic_init(&cancelled, 0);

    CBMFileResult **result_cache = calloc((size_t)(file_count > 0 ? file_count : 1),
                                          sizeof(CBMFileResult *));
    cbm_pipeline_ctx_t ctx = {
        .project_name = project_name,
        .repo_path = repo_path,
        .gbuf = gbuf,
        .registry = registry,
        .cancelled = &cancelled,
        .result_cache = result_cache,
        .prescan_cache = NULL,
        .prescan_count = 0,
        .prescan_path_map = NULL,
    };

    int rc = cbm_init();
    if (rc == 0) {
        rc = pass_structure(gbuf, project_name, files, file_count);
    }
    if (rc == 0) {
        rc = cbm_pipeline_pass_definitions(&ctx, files, file_count);
    }
    if (rc == 0) {
        rc = cbm_pipeline_pass_calls(&ctx, files, file_count);
    }
    if (rc == 0) {
        rc = cbm_pipeline_pass_usages(&ctx, files, file_count);
    }
    if (rc == 0) {
        rc = cbm_pipeline_pass_semantic(&ctx, files, file_count);
    }
    if (rc == 0) {
        rc = cbm_pipeline_pass_tests(&ctx, files, file_count);
    }
    if (rc == 0) {
        rc = cbm_pipeline_pass_githistory(&ctx);
    }
    if (rc == 0) {
        rc = cbm_gbuf_dump_to_json(gbuf, output_path);
    }

    free_result_cache(result_cache, file_count);
    cbm_registry_free(registry);
    cbm_gbuf_free(gbuf);
    free(project_name);
    cbm_discover_free(files, file_count);
    cbm_shutdown();

    if (rc != 0) {
        fprintf(stderr, "repo graph export failed\n");
        return 1;
    }
    return 0;
}
