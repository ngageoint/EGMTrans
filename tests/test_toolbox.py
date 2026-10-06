"""The ArcGIS Pro toolbox's parameter logic, run against a stand-in for arcpy:
the DTED index, profile and override parameters, their messages, and the
read-only summary of the header fields."""

import importlib.machinery
import importlib.util
import os
import sys
import types

TOOLBOX = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'arcgis', 'EGMTransToolbox.pyt')
PROFILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'samples', 'dted_profile_example.toml')


class _Filter:
    def __init__(self):
        self.type = None
        self.list = []


class _Parameter:
    """What a .pyt touches on arcpy.Parameter."""

    def __init__(self, displayName='', name='', datatype='', parameterType='', direction=''):
        self.displayName, self.name, self.datatype = displayName, name, datatype
        self.parameterType, self.direction = parameterType, direction
        self.filter = _Filter()
        self._columns = []
        self.filters = []
        self.value = None
        self.values = None
        self.enabled = True
        self.messages = []

    @property
    def columns(self):
        return self._columns

    @columns.setter
    def columns(self, columns):
        self._columns = columns
        self.filters = [_Filter() for _ in columns]

    @property
    def valueAsText(self):
        return None if self.value in (None, '') else str(self.value)

    def setErrorMessage(self, text):
        self.messages.append(('error', text))

    def setWarningMessage(self, text):
        self.messages.append(('warning', text))


def _load_toolbox(monkeypatch):
    fake = types.ModuleType('arcpy')
    fake.Parameter = _Parameter
    fake.AddMessage = fake.AddWarning = fake.AddError = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, 'arcpy', fake)
    loader = importlib.machinery.SourceFileLoader('egmtrans_toolbox', TOOLBOX)
    spec = importlib.util.spec_from_file_location('egmtrans_toolbox', TOOLBOX, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _validate(tool, params):
    """One validation pass as ArcGIS Pro runs it: messages cleared, then both hooks."""
    for param in params:
        param.messages = []
    tool.updateParameters(params)
    tool.updateMessages(params)


def _errors(param):
    return [text for kind, text in param.messages if kind == 'error']


def test_dted_parameters_messages_and_summary(monkeypatch, tmp_dir):
    toolbox = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    assert [p.name for p in params[13:]] == ['dted_index', 'dted_profile', 'dted_level', 'dted_naming',
                                             'dted_overrides', 'dted_summary', 'output_layer']
    index_p, profile_p, level_p, overrides_p, summary_p = params[13], params[14], params[15], params[17], params[18]
    assert overrides_p.datatype == 'GPValueTable' and overrides_p.filters[0].type == 'ValueList'
    assert 'producer_code' in overrides_p.filters[0].list and 'compilation_date' in overrides_p.filters[0].list
    assert 'vertical_datum' not in overrides_p.filters[0].list and 'source_id' not in overrides_p.filters[0].list
    assert summary_p.enabled is False and summary_p.datatype == 'GPString'

    params[0].value = os.path.join(tmp_dir, 'tiles')
    params[1].value = os.path.join(tmp_dir, 'out')
    _validate(tool, params)
    assert summary_p.value in (None, '') and not any(p.messages for p in params)

    # A profile alone, converting at its level: the summary says what it supplies, nothing is missing.
    profile_p.value = PROFILE
    level_p.value = '2'
    _validate(tool, params)
    assert summary_p.value.startswith('no index; level 2; from the profile: security_code, security_handling')
    assert summary_p.value.endswith('missing: none') and not any(p.messages for p in params)

    # A bad override is an error on its own parameter; a good one shows in the summary.
    overrides_p.values = [['dted_level', '2']]
    _validate(tool, params)
    assert _errors(overrides_p) and 'use --dted-level' in _errors(overrides_p)[0]
    overrides_p.values = [['producer_code', 'USNGA'], ['', '']]
    _validate(tool, params)
    assert not any(p.messages for p in params) and 'overridden: producer_code' in summary_p.value

    # The profile is for level 2: level 0 is an error on the profile.
    level_p.value = '0'
    _validate(tool, params)
    assert any('level' in text for text in _errors(profile_p))

    # Overrides alone cannot complete a header made from scratch: an error names the missing fields.
    profile_p.value = None
    level_p.value = '2'
    _validate(tool, params)
    assert any('security_code' in text and 'Nothing supplies' in text for text in _errors(profile_p))
    assert summary_p.value.startswith('no index; level 2; overridden: producer_code; missing: security_code')

    # The same overrides on a DTED-to-DTED run (nothing is made from scratch) are fine.
    level_p.value = None
    params[0].value = os.path.join(tmp_dir, 'n06e126.dt0')
    params[1].value = os.path.join(tmp_dir, 'out.dt0')
    _validate(tool, params)
    assert not any(p.messages for p in params)

    # An index: cells, NULL accuracies as a warning, and a missing file as an error.
    from egmtrans.dted.index import new_row, write_index

    index_path = os.path.join(tmp_dir, 'index.gpkg')
    write_index(index_path, {
        'N06E126': new_row('N06E126', security_code='U', data_edition=1, match_merge_version='A',
                           compilation_date='2024-07', abs_vert_acc=None, abs_horiz_acc=3),
        'N07E126': new_row('N07E126', security_code='U', data_edition=1, match_merge_version='A',
                           compilation_date='2024-07', abs_vert_acc=5, abs_horiz_acc=3),
    }, level=0)
    index_p.value = index_path
    _validate(tool, params)
    assert summary_p.value.startswith('2 cell(s); level 0; from the index: security_code, data_edition')
    assert 'abs_vert_acc (NULL in 1)' in summary_p.value
    assert 'abs_horiz_acc' in summary_p.value and _errors(index_p) == []
    assert any(kind == 'warning' and 'NULL accuracy' in text for kind, text in index_p.messages)
    index_p.value = os.path.join(tmp_dir, 'missing.gpkg')
    _validate(tool, params)
    assert _errors(index_p) and 'not found' in _errors(index_p)[0].lower()
