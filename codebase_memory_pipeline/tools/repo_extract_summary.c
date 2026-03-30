#include "discover/discover.h"
#include "cbm.h"
#include "lang_specs.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void json_string(FILE *out, const char *s) {
    fputc('"', out);
    if (!s) {
        fputc('"', out);
        return;
    }

    for (const unsigned char *p = (const unsigned char *)s; *p; ++p) {
        switch (*p) {
        case '\\':
            fputs("\\\\", out);
            break;
        case '"':
            fputs("\\\"", out);
            break;
        case '\n':
            fputs("\\n", out);
            break;
        case '\r':
            fputs("\\r", out);
            break;
        case '\t':
            fputs("\\t", out);
            break;
        default:
            if (*p < 0x20) {
                fprintf(out, "\\u%04x", *p);
            } else {
                fputc(*p, out);
            }
        }
    }

    fputc('"', out);
}

static char *read_file(const char *path, int *out_len) {
    FILE *f = fopen(path, "rb");
    long size;
    char *buf;

    if (!f) {
        return NULL;
    }

    (void)fseek(f, 0, SEEK_END);
    size = ftell(f);
    (void)fseek(f, 0, SEEK_SET);

    if (size < 0 || size > (long)(20 * 1024 * 1024)) {
        (void)fclose(f);
        return NULL;
    }

    buf = malloc((size_t)size + 1);
    if (!buf) {
        (void)fclose(f);
        return NULL;
    }

    if ((long)fread(buf, 1, (size_t)size, f) != size) {
        free(buf);
        (void)fclose(f);
        return NULL;
    }

    buf[size] = '\0';
    (void)fclose(f);

    if (out_len) {
        *out_len = (int)size;
    }

    return buf;
}

static void print_string_array(FILE *out, const char *const *items, int limit) {
    int i;

    fputc('[', out);
    if (!items) {
        fputc(']', out);
        return;
    }

    for (i = 0; items[i] && i < limit; i++) {
        if (i > 0) {
            fputc(',', out);
        }
        json_string(out, items[i]);
    }
    fputc(']', out);
}

static void print_defs(FILE *out, const CBMFileResult *r, int limit) {
    int i;

    fputc('[', out);
    for (i = 0; i < r->defs.count && i < limit; i++) {
        const CBMDefinition *d = &r->defs.items[i];
        if (i > 0) {
            fputc(',', out);
        }
        fputc('{', out);
        fputs("\"label\":", out);
        json_string(out, d->label);
        fputs(",\"name\":", out);
        json_string(out, d->name);
        fputs(",\"qualified_name\":", out);
        json_string(out, d->qualified_name);
        fputs(",\"parent_class\":", out);
        json_string(out, d->parent_class);
        fputs(",\"start_line\":", out);
        fprintf(out, "%u", d->start_line);
        fputs(",\"end_line\":", out);
        fprintf(out, "%u", d->end_line);
        fputs(",\"signature\":", out);
        json_string(out, d->signature);
        fputs(",\"complexity\":", out);
        fprintf(out, "%d", d->complexity);
        fputs(",\"lines\":", out);
        fprintf(out, "%d", d->lines);
        fputs(",\"is_test\":", out);
        fputs(d->is_test ? "true" : "false", out);
        fputs(",\"is_entry_point\":", out);
        fputs(d->is_entry_point ? "true" : "false", out);
        fputc('}', out);
    }
    fputc(']', out);
}

static void print_calls(FILE *out, const CBMFileResult *r, int limit) {
    int i;

    fputc('[', out);
    for (i = 0; i < r->calls.count && i < limit; i++) {
        const CBMCall *c = &r->calls.items[i];
        if (i > 0) {
            fputc(',', out);
        }
        fputc('{', out);
        fputs("\"callee_name\":", out);
        json_string(out, c->callee_name);
        fputs(",\"enclosing_func_qn\":", out);
        json_string(out, c->enclosing_func_qn);
        fputc('}', out);
    }
    fputc(']', out);
}

static void print_imports(FILE *out, const CBMFileResult *r, int limit) {
    int i;

    fputc('[', out);
    for (i = 0; i < r->imports.count && i < limit; i++) {
        const CBMImport *imp = &r->imports.items[i];
        if (i > 0) {
            fputc(',', out);
        }
        fputc('{', out);
        fputs("\"local_name\":", out);
        json_string(out, imp->local_name);
        fputs(",\"module_path\":", out);
        json_string(out, imp->module_path);
        fputc('}', out);
    }
    fputc(']', out);
}

int main(int argc, char **argv) {
    const char *repo_path;
    int limit = 20;
    cbm_discover_opts_t opts = {0};
    cbm_file_info_t *files = NULL;
    int file_count = 0;
    int processed = 0;
    int supported = 0;
    int total_defs = 0;
    int total_calls = 0;
    int total_imports = 0;
    int total_errors = 0;
    int i;

    if (argc < 2 || argc > 3) {
        fprintf(stderr, "usage: %s <repo-path> [limit]\n", argv[0]);
        return 2;
    }

    repo_path = argv[1];
    if (argc == 3) {
        limit = atoi(argv[2]);
        if (limit <= 0) {
            limit = 20;
        }
    }

    opts.mode = CBM_MODE_FULL;
    opts.ignore_file = NULL;
    opts.max_file_size = 0;

    if (cbm_discover(repo_path, &opts, &files, &file_count) != 0) {
        fprintf(stderr, "failed to discover files under %s\n", repo_path);
        return 1;
    }

    if (cbm_init() != 0) {
        cbm_discover_free(files, file_count);
        fprintf(stderr, "failed to initialize extraction engine\n");
        return 1;
    }

    fputs("{\"repo_path\":", stdout);
    json_string(stdout, repo_path);
    fputs(",\"discovered_files\":", stdout);
    fprintf(stdout, "%d", file_count);
    fputs(",\"limit\":", stdout);
    fprintf(stdout, "%d", limit);
    fputs(",\"files\":[", stdout);

    for (i = 0; i < file_count && processed < limit; i++) {
        char *source;
        int source_len = 0;
        CBMFileResult *r;
        const TSLanguage *ts_lang;

        source = read_file(files[i].path, &source_len);
        if (!source) {
            continue;
        }

        ts_lang = cbm_ts_language(files[i].language);
        r = cbm_extract_file(source, source_len, files[i].language, "demo", files[i].rel_path, 0,
                             NULL, NULL);
        free(source);

        if (processed > 0) {
            fputc(',', stdout);
        }
        processed++;

        fputc('{', stdout);
        fputs("\"rel_path\":", stdout);
        json_string(stdout, files[i].rel_path);
        fputs(",\"language\":", stdout);
        json_string(stdout, cbm_language_name(files[i].language));
        fputs(",\"supported\":", stdout);
        fputs(ts_lang ? "true" : "false", stdout);
        fputs(",\"size\":", stdout);
        fprintf(stdout, "%lld", (long long)files[i].size);

        if (!r) {
            total_errors++;
            fputs(",\"has_error\":true,\"error\":\"extract failed\"}", stdout);
            continue;
        }

        if (ts_lang && !r->has_error) {
            supported++;
            total_defs += r->defs.count;
            total_calls += r->calls.count;
            total_imports += r->imports.count;
        } else if (r->has_error) {
            total_errors++;
        }

        fputs(",\"has_error\":", stdout);
        fputs(r->has_error ? "true" : "false", stdout);
        fputs(",\"error\":", stdout);
        json_string(stdout, r->error_msg);
        fputs(",\"module_qn\":", stdout);
        json_string(stdout, r->module_qn);
        fputs(",\"is_test_file\":", stdout);
        fputs(r->is_test_file ? "true" : "false", stdout);
        fputs(",\"counts\":{", stdout);
        fprintf(stdout, "\"defs\":%d,\"calls\":%d,\"imports\":%d,\"usages\":%d",
                r->defs.count, r->calls.count, r->imports.count, r->usages.count);
        fputs("}", stdout);
        fputs(",\"defs\":", stdout);
        print_defs(stdout, r, 8);
        fputs(",\"calls\":", stdout);
        print_calls(stdout, r, 8);
        fputs(",\"imports\":", stdout);
        print_imports(stdout, r, 8);
        fputs(",\"exports\":", stdout);
        print_string_array(stdout, r->exports, 8);
        fputc('}', stdout);

        cbm_free_result(r);
    }

    fputs("],\"totals\":{", stdout);
    fprintf(stdout,
            "\"processed\":%d,\"supported\":%d,\"errors\":%d,"
            "\"defs\":%d,\"calls\":%d,\"imports\":%d",
            processed, supported, total_errors, total_defs, total_calls, total_imports);
    fputs("}}\n", stdout);

    cbm_shutdown();
    cbm_discover_free(files, file_count);
    return 0;
}
