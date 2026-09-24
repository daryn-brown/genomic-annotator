"""Subprocess verification of the real CLI using a disposable synthetic workflow."""

import unittest

from tests.synthetic_workflow import run_synthetic_workflow


class EndToEndTests(unittest.TestCase):
    """Exercise actual main.py processes rather than only an in-process CLI runner."""

    def test_synthetic_offline_mocked_online_and_cache_lifecycle(self) -> None:
        """All smoke commands must preserve called rows and persist/cache/clear safely."""
        results = run_synthetic_workflow()
        self.assertEqual(len(results), 5)
        self.assertTrue(all(result.startswith("PASS:") for result in results))


if __name__ == "__main__":
    unittest.main()
