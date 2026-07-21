from __future__ import annotations

import runpy
import unittest
from pathlib import Path


SUMMARIZE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate_image_sorting.py")
)["summarize"]


class AIEvaluationTests(unittest.TestCase):
    def test_release_gate_uses_accuracy_and_reports_coverage(self) -> None:
        records = [
            {"auto_ready": index < 40, "correct": index != 39}
            for index in range(100)
        ]

        summary = SUMMARIZE(records)

        self.assertEqual(summary["auto_ready_count"], 40)
        self.assertEqual(summary["auto_ready_accuracy"], 0.975)
        self.assertEqual(summary["coverage"], 0.4)
        self.assertTrue(summary["release_gate_passed"])


if __name__ == "__main__":
    unittest.main()
