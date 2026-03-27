int tf_pass_count = 0;
int tf_fail_count = 0;
int tf_skip_count = 0;

#include "test_framework.h"

extern void suite_extraction(void);

int main(void) {
    printf("\n  codebase_memory_pipeline extraction test suite\n");

    RUN_SUITE(extraction);

    TEST_SUMMARY();
}
