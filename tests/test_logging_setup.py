"""The log: PROGRESS lines, the quiet messages pane of a large batch in
ArcGIS Pro, and the lines that go to the file only."""

import logging
import os

from egmtrans import _state
from egmtrans.logging_setup import PROGRESS, end_logger, log_to_file_only, log_traceback, progress, setup_logger


class _Arcpy:
    def __init__(self):
        self.messages = []

    def AddMessage(self, text):
        self.messages.append(('message', text))

    def AddWarning(self, text):
        self.messages.append(('warning', text))

    def AddError(self, text):
        self.messages.append(('error', text))


def test_quiet_mode_keeps_info_out_of_the_messages_pane_but_in_the_file(tmp_dir):
    fake = _Arcpy()
    _state.set_arcpy(fake)
    try:
        log_file = os.path.join(tmp_dir, 'run.log')
        logger = setup_logger(log_file, True, is_arc_mode=True)
        logger.info('detail one')
        _state.set_quiet(True)
        logger.info('detail two')
        progress('Wrote cell two')
        logger.warning('a warning')
        _state.set_quiet(False)
        logger.info('detail three')
        log_to_file_only('for the file only')
        try:
            raise RuntimeError('kaboom')
        except RuntimeError:
            log_traceback()
        end_logger(save_log=True)
    finally:
        _state.set_arcpy(None)
        _state.set_quiet(False)
    texts = [text for _kind, text in fake.messages]
    assert 'detail one' in texts and 'detail three' in texts
    assert 'detail two' not in texts, 'INFO is kept out of the pane in quiet mode'
    assert 'Wrote cell two' in texts and 'WARNING: a warning' in texts
    assert not any('for the file only' in text or 'kaboom' in text for text in texts)
    with open(log_file, encoding='utf-8') as handle:
        log = handle.read()
    for expected in ('run started', 'detail one', 'detail two', 'Wrote cell two', 'a warning', 'detail three',
                     'for the file only', 'Traceback (for the record)', 'kaboom'):
        assert expected in log, expected
    assert logging.getLevelName(PROGRESS) == 'PROGRESS'


def test_a_run_without_a_log_file_does_not_delete_the_previous_runs_log(tmp_dir):
    fake = _Arcpy()
    _state.set_arcpy(fake)
    _state.set_arc_mode(True)
    try:
        log_file = os.path.join(tmp_dir, 'first.log')
        setup_logger(log_file, True, is_arc_mode=True)
        end_logger(save_log=True)
        assert os.path.isfile(log_file)
        setup_logger(None, False, is_arc_mode=True)
        end_logger(save_log=False)
        assert os.path.isfile(log_file), 'the second run had no log of its own and must not remove the first'
    finally:
        _state.set_arcpy(None)
        _state.set_arc_mode(False)
