from pathlib import Path
import unittest


def test_add(left, right):
    return left + right


class ArithmeticTests(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(test_add(20, 22), 42)

    def test_negative_values(self):
        self.assertEqual(test_add(-1, 1), 0)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ArithmeticTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    Path("build").mkdir(exist_ok=True)
    Path("build/test-summary.txt").write_text(
        f"tests_run={result.testsRun}\nfailures={len(result.failures)}\nerrors={len(result.errors)}\n",
        encoding="utf-8",
    )
