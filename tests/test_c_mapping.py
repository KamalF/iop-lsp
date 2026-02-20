"""Tests for IOP <-> C name conversions."""

import unittest

from iop_lsp.c_mapping import (
    c_to_camelcase,
    camelcase_to_c,
    iop_type_to_c,
)


class TestCamelcaseToC(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(camelcase_to_c('MyStruct'), 'my_struct')

    def test_trailing_upper(self):
        self.assertEqual(camelcase_to_c('MyStructA'), 'my_struct_a')

    def test_consecutive_uppers(self):
        self.assertEqual(camelcase_to_c('HTTPCode'), 'http_code')

    def test_single_word(self):
        self.assertEqual(camelcase_to_c('Foo'), 'foo')

    def test_all_upper(self):
        self.assertEqual(camelcase_to_c('URL'), 'url')

    def test_mixed(self):
        self.assertEqual(camelcase_to_c('ClassDso'), 'class_dso')

    def test_digits(self):
        self.assertEqual(camelcase_to_c('MyStruct2'), 'my_struct2')

    def test_digit_then_upper(self):
        self.assertEqual(camelcase_to_c('V2Request'), 'v2_request')


class TestCToCamelcase(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(c_to_camelcase('my_struct'), 'MyStruct')

    def test_trailing(self):
        self.assertEqual(c_to_camelcase('my_struct_a'), 'MyStructA')

    def test_single(self):
        self.assertEqual(c_to_camelcase('foo'), 'Foo')

    def test_multi_segment(self):
        self.assertEqual(c_to_camelcase('class_dso'), 'ClassDso')


class TestIopTypeToC(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            iop_type_to_c('tstiop.MyStructA'), 'tstiop__my_struct_a'
        )

    def test_nested_package(self):
        self.assertEqual(
            iop_type_to_c('test.dso.ClassDso'), 'test__dso__class_dso'
        )

    def test_single_name(self):
        self.assertEqual(iop_type_to_c('core.LogLevel'), 'core__log_level')

    def test_simple_package(self):
        self.assertEqual(iop_type_to_c('foo.Bar'), 'foo__bar')


if __name__ == '__main__':
    unittest.main()
