# -*- coding: utf-8 -*-
"""`calc.py` 的验收测试。修复前应当失败。"""
import unittest

from calc import add, mul


class TestCalc(unittest.TestCase):
    def test_add_positive(self):
        self.assertEqual(add(1, 2), 3)

    def test_add_negative(self):
        self.assertEqual(add(-3, -4), -7)

    def test_add_zero(self):
        self.assertEqual(add(0, 5), 5)

    def test_mul(self):
        self.assertEqual(mul(3, 4), 12)


if __name__ == "__main__":
    unittest.main()
