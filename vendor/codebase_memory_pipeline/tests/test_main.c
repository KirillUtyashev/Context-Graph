int tf_pass_count = 0;
int tf_fail_count = 0;
int tf_skip_count = 0;

#include "test_framework.h"

extern void suite_language(void);
extern void suite_gitignore(void);
extern void suite_discover(void);
extern void suite_registry(void);

int main(void) {
    printf("\n  codebase_memory_pipeline starter test suite\n");

    RUN_SUITE(language);
    RUN_SUITE(gitignore);
    RUN_SUITE(discover);
    RUN_SUITE(registry);

    TEST_SUMMARY();
}
