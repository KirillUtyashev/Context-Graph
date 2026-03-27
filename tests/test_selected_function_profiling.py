from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from context_graph.profiling.selected_functions import ALL_TARGETS, run_profiles


class SelectedFunctionProfilingTests(unittest.TestCase):
    def test_run_profiles_emits_reports_for_all_targets(self):
        with tempfile.TemporaryDirectory(prefix="context-graph-profile-test-") as tmp_dir:
            results = run_profiles(
                targets=ALL_TARGETS,
                output_dir=tmp_dir,
                scale="small",
                repeat_multiplier=1,
                top_n=10,
            )

            self.assertEqual(len(results), len(ALL_TARGETS))
            for result in results:
                prof_path = Path(result.output_prof)
                txt_path = Path(result.output_txt)
                json_path = Path(result.output_json)

                self.assertTrue(prof_path.exists(), msg=f"missing {prof_path}")
                self.assertTrue(txt_path.exists(), msg=f"missing {txt_path}")
                self.assertTrue(json_path.exists(), msg=f"missing {json_path}")

                report_text = txt_path.read_text(encoding="utf-8")
                self.assertIn("Ordered by cumulative time", report_text)
                self.assertIn(result.function_name, report_text)

                payload = json.loads(json_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["target"], result.target)
                self.assertEqual(payload["function_name"], result.function_name)
                self.assertGreaterEqual(payload["wall_time_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
