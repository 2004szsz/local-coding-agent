# -*- coding: utf-8 -*-
import unittest

from tools.calculator import evaluate


class CalculatorTests(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(evaluate("(12 + 3) * 4 / 2"), "30")

    def test_rejects_names(self):
        with self.assertRaises(ValueError):
            evaluate("__import__('os').system('echo hi')")

    def test_rejects_large_power(self):
        with self.assertRaises(ValueError):
            evaluate("9 ** 99")

    def test_empty(self):
        with self.assertRaises(ValueError):
            evaluate("  ")


if __name__ == "__main__":
    unittest.main()
