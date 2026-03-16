"""Tests for egmtrans.cli — str2bool, process_file signature, etc."""

import argparse
import pytest

from egmtrans.cli import delete_output_directory, process_file, str2bool


class TestStr2Bool:
    def test_true_values(self):
        for v in ('yes', 'true', 't', 'y', '1', 'YES', 'True', 'T', 'Y'):
            assert str2bool(v) is True

    def test_false_values(self):
        for v in ('no', 'false', 'f', 'n', '0', 'NO', 'False', 'F', 'N'):
            assert str2bool(v) is False

    def test_bool_passthrough(self):
        assert str2bool(True) is True
        assert str2bool(False) is False

    def test_none_returns_false(self):
        assert str2bool(None) is False

    def test_invalid_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            str2bool('maybe')

    def test_empty_string_raises(self):
        with pytest.raises(argparse.ArgumentTypeError):
            str2bool('')


class TestProcessFileSignature:
    def test_callable(self):
        assert callable(process_file)


class TestDeleteOutputDirectory:
    def test_callable(self):
        assert callable(delete_output_directory)
