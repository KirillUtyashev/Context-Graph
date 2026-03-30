CC ?= cc
CXX ?= c++

CFLAGS_COMMON = -std=c11 -D_DEFAULT_SOURCE -Wall -Wextra -Werror \
	-Isrc -Isrc/foundation -Iinternal/cbm -Iinternal/cbm/vendored/ts_runtime/include
CFLAGS_EXTRACT = -std=c11 -D_DEFAULT_SOURCE -Wall -Wextra -Wno-unused-function -Wno-unused-parameter \
	-Isrc -Isrc/foundation -Iinternal/cbm -Iinternal/cbm/vendored/ts_runtime/include
CXXFLAGS_EXTRACT = -std=c++17 -D_DEFAULT_SOURCE -Wall -Wextra -Wno-unused-parameter \
	-Isrc -Isrc/foundation -Iinternal/cbm -Iinternal/cbm/vendored/ts_runtime/include
LDFLAGS_COMMON = -lm
LDFLAGS_EXTRACT = -lm -lz

BUILD_DIR = build
OBJ_DIR = $(BUILD_DIR)/obj
STARTER_BIN = $(BUILD_DIR)/test-runner
EXTRACTION_BIN = $(BUILD_DIR)/test-extraction
SUMMARY_BIN = $(BUILD_DIR)/repo-extract-summary
EXPORT_BIN = $(BUILD_DIR)/repo-graph-export

STARTER_FOUNDATION_SRCS = \
	src/foundation/hash_table.c \
	src/foundation/compat.c \
	src/foundation/compat_fs.c

STARTER_DISCOVER_SRCS = \
	src/discover/language.c \
	src/discover/gitignore.c \
	src/discover/discover.c

STARTER_PIPELINE_SRCS = \
	src/pipeline/fqn.c \
	src/pipeline/registry.c \
	src/pipeline/confidence_band.c

STARTER_TEST_SRCS = \
	tests/test_main.c \
	tests/test_language.c \
	tests/test_gitignore.c \
	tests/test_discover.c \
	tests/test_registry.c

STARTER_ALL_SRCS = $(STARTER_FOUNDATION_SRCS) $(STARTER_DISCOVER_SRCS) $(STARTER_PIPELINE_SRCS) $(STARTER_TEST_SRCS)

EXTRACTION_LIB_C_SRCS = \
	src/foundation/compat.c \
	internal/cbm/arena.c \
	internal/cbm/cbm.c \
	internal/cbm/extract_calls.c \
	internal/cbm/extract_defs.c \
	internal/cbm/extract_env_accesses.c \
	internal/cbm/extract_imports.c \
	internal/cbm/extract_semantic.c \
	internal/cbm/extract_type_assigns.c \
	internal/cbm/extract_type_refs.c \
	internal/cbm/extract_unified.c \
	internal/cbm/extract_usages.c \
	internal/cbm/helpers.c \
	internal/cbm/lang_specs.c \
	internal/cbm/lsp_all.c \
	internal/cbm/ts_runtime.c \
	internal/cbm/grammar_stubs.c \
	internal/cbm/grammar_python.c \
	internal/cbm/grammar_c.c \
	internal/cbm/grammar_cpp.c \
	internal/cbm/grammar_java.c \
	internal/cbm/grammar_javascript.c \
	internal/cbm/grammar_typescript.c \
	internal/cbm/grammar_go.c \
	internal/cbm/grammar_bash.c \
	internal/cbm/grammar_rust.c \
	internal/cbm/grammar_c_sharp.c

EXTRACTION_TEST_C_SRCS = \
	tests/test_extraction.c \
	tests/test_extraction_main.c

EXTRACTION_C_SRCS = $(EXTRACTION_LIB_C_SRCS) $(EXTRACTION_TEST_C_SRCS)

EXTRACTION_CPP_SRCS = \
	internal/cbm/preprocessor.cpp

EXTRACTION_OBJS = $(patsubst %.c,$(OBJ_DIR)/%.o,$(EXTRACTION_C_SRCS)) $(patsubst %.cpp,$(OBJ_DIR)/%.o,$(EXTRACTION_CPP_SRCS))
EXTRACTION_LIB_OBJS = $(patsubst %.c,$(OBJ_DIR)/%.o,$(EXTRACTION_LIB_C_SRCS)) $(patsubst %.cpp,$(OBJ_DIR)/%.o,$(EXTRACTION_CPP_SRCS))

SUMMARY_C_SRCS = \
	src/foundation/compat_fs.c \
	src/discover/language.c \
	src/discover/gitignore.c \
	src/discover/discover.c \
	tools/repo_extract_summary.c

SUMMARY_OBJS = $(EXTRACTION_LIB_OBJS) $(patsubst %.c,$(OBJ_DIR)/%.o,$(SUMMARY_C_SRCS))

EXPORT_C_SRCS = \
	src/foundation/hash_table.c \
	src/foundation/compat_fs.c \
	src/discover/language.c \
	src/discover/gitignore.c \
	src/discover/discover.c \
	src/graph_buffer/graph_buffer.c \
	src/pipeline/fqn.c \
	src/pipeline/registry.c \
	src/pipeline/confidence_band.c \
	src/pipeline/pass_definitions.c \
	src/pipeline/pass_calls.c \
	src/pipeline/pass_usages.c \
	src/pipeline/pass_semantic.c \
	src/pipeline/pass_tests.c \
	src/pipeline/pass_githistory.c \
	src/store/store_stub.c \
	internal/cbm/sqlite_writer_stub.c \
	tools/repo_graph_export.c

EXPORT_OBJS = $(EXTRACTION_LIB_OBJS) $(patsubst %.c,$(OBJ_DIR)/%.o,$(EXPORT_C_SRCS))

.PHONY: test test-starter test-extraction repo-summary repo-graph-export clean

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(STARTER_BIN): $(STARTER_ALL_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS_COMMON) -o $@ $(STARTER_ALL_SRCS) $(LDFLAGS_COMMON)

$(OBJ_DIR)/%.o: %.c | $(BUILD_DIR)
	mkdir -p $(dir $@)
	$(CC) $(CFLAGS_EXTRACT) -c $< -o $@

$(OBJ_DIR)/%.o: %.cpp | $(BUILD_DIR)
	mkdir -p $(dir $@)
	$(CXX) $(CXXFLAGS_EXTRACT) -c $< -o $@

$(EXTRACTION_BIN): $(EXTRACTION_OBJS) | $(BUILD_DIR)
	$(CXX) -o $@ $(EXTRACTION_OBJS) $(LDFLAGS_EXTRACT)

$(SUMMARY_BIN): $(SUMMARY_OBJS) | $(BUILD_DIR)
	$(CXX) -o $@ $(SUMMARY_OBJS) $(LDFLAGS_EXTRACT)

$(EXPORT_BIN): $(EXPORT_OBJS) | $(BUILD_DIR)
	$(CXX) -o $@ $(EXPORT_OBJS) $(LDFLAGS_EXTRACT)

test-starter: $(STARTER_BIN)
	cd $(CURDIR) && ./$(STARTER_BIN)

test-extraction: $(EXTRACTION_BIN)
	cd $(CURDIR) && ./$(EXTRACTION_BIN)

repo-summary: $(SUMMARY_BIN)
	@echo "built $(SUMMARY_BIN)"

repo-graph-export: $(EXPORT_BIN)
	@echo "built $(EXPORT_BIN)"

test: test-starter test-extraction

clean:
	rm -rf $(BUILD_DIR)
