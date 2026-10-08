"""The ArcGIS Pro toolbox's parameter logic, run against a stand-in for arcpy:
the Output Format refresh, the datum and algorithm lists, the DTED group, the
naming dropdown and template, the water-body toggles, the DTED index, profile
and override messages, the read-only summary, the execute path and the
display names shared with the XML help files."""

import importlib.machinery
import importlib.util
import os
import re
import sys
import types
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from egmtrans.batch import BatchResult

ROOT = os.path.dirname(os.path.dirname(__file__))
TOOLBOX = os.path.join(ROOT, 'arcgis', 'EGMTransToolbox.pyt')
PROFILE = os.path.join(ROOT, 'tests', 'data', 'dted_profile.toml')

requires_grids = pytest.mark.skipif(
    not os.path.isfile(os.path.join(ROOT, 'datums', 'us_nga_egm96_1.tif')), reason='the EGM96 grid is not present'
)


@pytest.fixture(autouse=True)
def _reset_arc_state():
    """execute() puts the package in ArcGIS mode with the fake arcpy; the next test must not inherit that."""
    from egmtrans import _state

    yield
    _state.set_arc_mode(False)
    _state.set_arcpy(None)
    _state.set_quiet(False)


class _Filter:
    def __init__(self):
        self.type = None
        self.list = []


class _Parameter:
    """What a .pyt touches on arcpy.Parameter."""

    def __init__(self, displayName='', name='', datatype='', parameterType='', direction='', category=None):
        self.displayName, self.name, self.datatype = displayName, name, datatype
        self.parameterType, self.direction, self.category = parameterType, direction, category
        self.filter = _Filter()
        self._columns = []
        self.filters = []
        self.value = None
        self.values = None
        self.enabled = True
        self.altered = False
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

    def clearMessage(self):
        self.messages = []


class _Layer:
    def __init__(self, path):
        self.path = path

    def getOutput(self, index):
        return f'layer:{self.path}'


def _fake_arcpy():
    fake = types.ModuleType('arcpy')
    fake.Parameter = _Parameter
    fake.messages = []
    fake.AddMessage = lambda text: fake.messages.append(('message', str(text)))
    fake.AddWarning = lambda text: fake.messages.append(('warning', str(text)))
    fake.AddError = lambda text: fake.messages.append(('error', str(text)))
    fake.env = types.SimpleNamespace(overwriteOutput=False, isCancelled=False)
    fake.set_parameters = []
    fake.SetParameter = lambda index, value: fake.set_parameters.append((index, value))
    fake.management = types.SimpleNamespace(
        calls=[],
        CalculateStatistics=lambda path: fake.management.calls.append(('stats', path)),
        MakeRasterLayer=lambda path, name: (fake.management.calls.append(('layer', path)), _Layer(path))[1],
    )
    return fake


def _load_toolbox(monkeypatch):
    fake = _fake_arcpy()
    monkeypatch.setitem(sys.modules, 'arcpy', fake)
    loader = importlib.machinery.SourceFileLoader('egmtrans_toolbox', TOOLBOX)
    spec = importlib.util.spec_from_file_location('egmtrans_toolbox', TOOLBOX, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    module._NOTES.clear()
    module._METADATA_CACHE.clear()
    return module, fake


class _Messages:
    def __init__(self):
        self.lines = []

    def addMessage(self, text):
        self.lines.append(str(text))


def _validate(tool, params):
    """One validation pass as ArcGIS Pro runs it: messages cleared, then both hooks."""
    for param in params:
        param.messages = []
    tool.updateParameters(params)
    tool.updateMessages(params)


def _by_name(params):
    return {param.name: param for param in params}


def _errors(param):
    return [text for kind, text in param.messages if kind == 'error']


def _warnings(param):
    return [text for kind, text in param.messages if kind == 'warning']


def _write_dted0(path, lon0, lat0, datum_code='E96', heights=200):
    """A DTED0 cell written with the package's own codec, no GDAL defaults."""
    from egmtrans.dted.header import CellGeometry, new_header
    from egmtrans.dted.records import write_dted_file

    cell = CellGeometry(0, lon0, lat0)
    header = new_header(cell)
    header.set('dsi.security_code', 'U')
    header.set_raw('uhl.security_code', 'U  ')
    header.set('dsi.data_edition', 1)
    header.set('dsi.match_merge_version', 'A')
    header.set('dsi.producer_code', 'USNGA')
    header.set('dsi.compilation_date', '2024-07')
    header.set('dsi.vertical_datum', datum_code)
    for key in ('acc.abs_horiz_acc', 'acc.abs_vert_acc', 'acc.rel_horiz_acc', 'acc.rel_vert_acc', 'uhl.abs_vert_acc'):
        header.set(key, 5)
    values = np.full((cell.lat_points, cell.lon_lines), heights, dtype=np.int32)
    write_dted_file(path, header, values)
    return path


def test_parameter_names_types_and_defaults(monkeypatch):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    assert [p.name for p in params] == [
        'input', 'output', 'output_format', 'source_datum', 'target_datum', 'algorithm', 'min_patch_size',
        'flatten', 'create_mask', 'save_log', 'skip_existing', 'context_folder', 'water_levels', 'containment',
        'abs_horiz_accuracy', 'dted_index', 'dted_profile', 'dted_naming', 'dted_naming_template',
        'dted_overrides', 'dted_summary', 'output_layer',
    ]
    p = _by_name(params)
    assert p['output_format'].filter.list == ['DTED2', 'DTED1', 'DTED0', 'GeoTIFF']
    assert p['output_format'].value == 'DTED2' and p['output_format'].parameterType == 'Required'
    assert p['source_datum'].value is None, 'the user states what they start with'
    assert p['target_datum'].value == 'EGM96' and p['target_datum'].filter.list == ['EGM96']
    assert p['algorithm'].filter.list == ['Bilinear Interpolation']
    assert p['dted_naming'].filter.list == ['DTED standard', 'Cell name', 'Input name', 'Custom template']
    assert p['dted_naming'].value == 'DTED standard'
    assert p['dted_naming_template'].enabled is False
    assert p['skip_existing'].value is False and p['skip_existing'].datatype == 'GPBoolean'
    assert p['dted_overrides'].datatype == 'GPValueTable' and p['dted_overrides'].filters[0].type == 'ValueList'
    assert 'producer_code' in p['dted_overrides'].filters[0].list
    assert 'vertical_datum' not in p['dted_overrides'].filters[0].list
    assert p['dted_summary'].enabled is False and p['dted_summary'].datatype == 'GPString'
    for name in toolbox.DTED_GROUP:
        assert p[name].category == 'DTED output', name
    for name in ('input', 'output', 'output_format', 'source_datum', 'target_datum', 'algorithm', 'min_patch_size',
                 'flatten', 'create_mask', 'save_log', 'skip_existing', 'context_folder', 'water_levels',
                 'containment'):
        assert p[name].category is None, name
    assert set(toolbox.NAMING_LABELS.values()) <= set(toolbox.EGMTrans.DTED_NAMING_PRESETS)
    assert [tool_class.__name__ for tool_class in toolbox.Toolbox().tools] == [
        'Tool', 'DtedHeaderReport', 'DtedSelfTest', 'DtedDmed',
    ]


def test_output_format_refreshes_the_lists_and_the_dted_group(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = os.path.join(tmp_dir, 'tiles')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['source_datum'].value = 'EGM2008'
    _validate(tool, params)
    assert p['target_datum'].filter.list == ['EGM96'] and p['target_datum'].value == 'EGM96'
    assert p['algorithm'].filter.list == ['Bilinear Interpolation']
    assert all(p[name].enabled for name in toolbox.DTED_GROUP if name not in ('dted_summary', 'dted_naming_template'))
    assert not any(_errors(p[name]) for name in p)
    assert [name for name in p if p[name].messages] == ['dted_naming'], 'only the naming example is shown'

    p['output_format'].value = 'GeoTIFF'
    _validate(tool, params)
    assert p['target_datum'].filter.list == ['WGS84', 'EGM96', 'EGM2008']
    assert p['target_datum'].value == 'EGM96', 'the value stays; only the list grows'
    assert p['algorithm'].filter.list == ['Bilinear Interpolation', 'Thin Plate Spline', 'Delaunay Triangulation']
    assert not any(p[name].enabled for name in toolbox.DTED_GROUP)

    p['target_datum'].value = 'EGM2008'
    p['algorithm'].value = 'Thin Plate Spline'
    _validate(tool, params)
    assert not any(p[name].messages for name in p), 'a GeoTIFF run has no DTED message'
    p['output_format'].value = 'DTED1'
    _validate(tool, params)
    assert p['target_datum'].value == 'EGM96' and any('EGM96 only' in text for text in _warnings(p['target_datum']))
    assert p['algorithm'].value == 'Bilinear Interpolation' and _warnings(p['algorithm'])
    assert not _errors(p['target_datum']) and not _errors(p['algorithm'])
    _validate(tool, params)
    assert not p['target_datum'].messages, 'the note is shown once'


def test_output_extension_against_the_format(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = os.path.join(tmp_dir, 'tile.tif')
    p['source_datum'].value = 'EGM2008'
    cases = {
        ('DTED2', 'out.tif'): 'GeoTIFF name but the Output Format is DTED2',
        ('DTED2', 'out.dt1'): 'DTED level 1, not DTED2',
        ('GeoTIFF', 'out.dt2'): 'DTED name but the Output Format is GeoTIFF',
    }
    for (fmt, name), expected in cases.items():
        p['output_format'].value = fmt
        p['output'].value = os.path.join(tmp_dir, name)
        _validate(tool, params)
        assert any(expected in text for text in _errors(p['output'])), (fmt, name, p['output'].messages)
    for fmt, name in (('GeoTIFF', 'out.tiff'), ('GeoTIFF', 'out.tif'), ('DTED2', 'out.dt2'), ('DTED0', 'out.dt0')):
        p['output_format'].value = fmt
        p['output'].value = os.path.join(tmp_dir, name)
        _validate(tool, params)
        assert not p['output'].messages, (fmt, name, p['output'].messages)
    p['output_format'].value = 'DTED2'
    p['output'].value = os.path.join(tmp_dir, 'out.xyz')
    _validate(tool, params)
    assert any('treated as a folder' in text for text in _warnings(p['output'])) and not _errors(p['output'])


def test_a_dted_input_selects_its_level(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = os.path.join(tmp_dir, 'n06e126.dt0')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['source_datum'].value = 'EGM2008'
    _validate(tool, params)
    assert p['output_format'].value == 'DTED0' and not _errors(p['output_format'])
    p['output_format'].altered = True
    p['output_format'].value = 'GeoTIFF'
    _validate(tool, params)
    assert any('written as DTED at its level' in text for text in _errors(p['output_format']))
    p['output_format'].value = 'DTED2'
    _validate(tool, params)
    assert any('keeps its level' in text for text in _errors(p['output_format']))
    p['output_format'].value = 'DTED0'
    _validate(tool, params)
    assert not _errors(p['output_format'])


def test_target_datum_error_is_the_backstop(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = os.path.join(tmp_dir, 'tiles')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['source_datum'].value = 'EGM2008'
    p['target_datum'].value = 'EGM2008'  # a value pasted from History bypasses the list
    tool.updateMessages(params)
    assert any('MIL-PRF-89020B 3.2.2' in text for text in _errors(p['target_datum']))


def test_naming_dropdown_template_and_example(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = os.path.join(tmp_dir, 'tiles')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['source_datum'].value = 'EGM2008'
    _validate(tool, params)
    assert p['dted_naming_template'].enabled is False
    assert any('DTED/E006/N49.dt2' in text for text in _warnings(p['dted_naming']))
    assert toolbox._naming_value(p) == 'dted'

    p['dted_naming'].value = 'Cell name'
    _validate(tool, params)
    assert any('N49E006.dt2' in text for text in _warnings(p['dted_naming'])) and toolbox._naming_value(p) == 'cell'

    p['dted_naming'].value = 'Custom template'
    _validate(tool, params)
    assert p['dted_naming_template'].enabled is True
    assert any('Give a template' in text for text in _errors(p['dted_naming_template']))
    p['dted_naming_template'].value = '{lvl}'
    _validate(tool, params)
    assert any('uses {lvl}' in text for text in _errors(p['dted_naming_template']))
    p['dted_naming_template'].value = 'DTED{level}_{lon}{lat}'
    _validate(tool, params)
    assert not _errors(p['dted_naming_template'])
    assert any('DTED2_E006N49.dt2' in text for text in _warnings(p['dted_naming']))
    assert toolbox._naming_value(p) == 'DTED{level}_{lon}{lat}'

    p['output_format'].value = 'GeoTIFF'
    _validate(tool, params)
    assert p['dted_naming_template'].enabled is False and not p['dted_naming'].messages


def test_water_parameters_follow_flattening_and_the_datums(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = os.path.join(tmp_dir, 'tiles')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['output_format'].value = 'GeoTIFF'
    p['source_datum'].value = 'EGM2008'
    _validate(tool, params)
    assert all(p[name].enabled for name in toolbox.WATER_GROUP)
    p['flatten'].value = False
    _validate(tool, params)
    assert not any(p[name].enabled for name in toolbox.WATER_GROUP)
    p['flatten'].value = True
    p['source_datum'].value = 'WGS84'
    _validate(tool, params)
    assert not any(p[name].enabled for name in toolbox.WATER_GROUP)
    p['source_datum'].value = 'EGM2008'
    p['target_datum'].value = 'WGS84'
    _validate(tool, params)
    assert not any(p[name].enabled for name in toolbox.WATER_GROUP)
    p['target_datum'].value = 'EGM96'
    _validate(tool, params)
    assert all(p[name].enabled for name in toolbox.WATER_GROUP)


def test_a_cleared_minimum_patch_size_falls_back_to_the_default(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = os.path.join(tmp_dir, 'tiles')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['source_datum'].value = 'EGM2008'
    p['min_patch_size'].value = None
    _validate(tool, params)
    assert p['min_patch_size'].value == 16 and any('set to 16' in text for text in _warnings(p['min_patch_size']))


def test_source_datum_warns_when_the_file_declares_another(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = _write_dted0(os.path.join(tmp_dir, 'n06e126.dt0'), 126, 6, datum_code='E08')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['source_datum'].value = 'EGM96'
    _validate(tool, params)
    assert any('declares EGM2008' in text for text in _warnings(p['source_datum']))
    p['source_datum'].value = 'EGM2008'
    _validate(tool, params)
    assert not p['source_datum'].messages


def test_dted_parameters_messages_and_summary(monkeypatch, tmp_dir):
    toolbox, _fake = _load_toolbox(monkeypatch)
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    index_p, profile_p = p['dted_index'], p['dted_profile']
    overrides_p, summary_p = p['dted_overrides'], p['dted_summary']

    p['input'].value = os.path.join(tmp_dir, 'tiles')
    p['output'].value = os.path.join(tmp_dir, 'out')
    p['source_datum'].value = 'EGM2008'
    _validate(tool, params)
    assert summary_p.value in (None, '')
    assert not any(_errors(param) for param in params)

    # A profile alone, converting at its level: the summary says what it supplies, nothing is missing.
    profile_p.value = PROFILE
    _validate(tool, params)
    assert summary_p.value.startswith('no index; level 2; from the profile: security_code, security_handling')
    assert summary_p.value.endswith('missing: none') and not any(_errors(param) for param in params)

    # A bad override is an error on its own parameter; a good one shows in the summary.
    overrides_p.values = [['dted_level', '2']]
    _validate(tool, params)
    assert _errors(overrides_p) and 'use --dted-level' in _errors(overrides_p)[0]
    overrides_p.values = [['producer_code', 'USNGA'], ['', '']]
    _validate(tool, params)
    assert not any(_errors(param) for param in params) and 'overridden: producer_code' in summary_p.value

    # A date outside the readers' century is an error on the overrides.
    overrides_p.values = [['compilation_date', '1975-06']]
    _validate(tool, params)
    assert any('1980-2079' in text for text in _errors(overrides_p))
    overrides_p.values = [['producer_code', 'USNGA']]

    # The profile is for level 2: the DTED0 format is an error on the profile.
    p['output_format'].value = 'DTED0'
    _validate(tool, params)
    assert any('level' in text for text in _errors(profile_p))

    # Overrides alone cannot complete a header made from scratch: an error names the missing fields.
    profile_p.value = None
    p['output_format'].value = 'DTED2'
    _validate(tool, params)
    assert any('security_code' in text and 'Nothing supplies' in text for text in _errors(profile_p))
    assert summary_p.value.startswith('no index; level 2; overridden: producer_code; missing: security_code')

    # The same overrides on a DTED-to-DTED run (nothing is made from scratch) are fine.
    p['input'].value = os.path.join(tmp_dir, 'n06e126.dt0')
    p['output'].value = os.path.join(tmp_dir, 'out.dt0')
    _validate(tool, params)
    assert not any(_errors(param) for param in params), [(param.name, param.messages) for param in params]

    # An index: cells, NULL accuracies as a warning, and a missing file as an error on the index.
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
    assert any('NULL accuracy' in text for text in _warnings(index_p))
    index_p.value = os.path.join(tmp_dir, 'missing.gpkg')
    _validate(tool, params)
    assert _errors(index_p) and 'not found' in _errors(index_p)[0].lower() and not _errors(profile_p)

    # A profile that cannot be read is an error on the profile, not on the index.
    index_p.value = None
    bad_profile = os.path.join(tmp_dir, 'bad.toml')
    with open(bad_profile, 'w', encoding='utf-8') as handle:
        handle.write('schema = 1\n[product]\nproducer_code = "<fill in>"\n')
    profile_p.value = bad_profile
    _validate(tool, params)
    assert any('placeholder not filled' in text for text in _errors(profile_p)) and not _errors(index_p)

    # A producer code that collides with an ISO code is a warning on the profile.
    with open(bad_profile, 'w', encoding='utf-8') as handle:
        handle.write('schema = 1\n[product]\ndted_level = 0\nproducer_code = "GB"\n')
    _validate(tool, params)
    assert any('Gabon' in text for text in _warnings(profile_p))


def _prepare_tool(toolbox, tmp_dir, *, input_path, output_path, output_format, source, target='EGM96'):
    tool = toolbox.Tool()
    params = tool.getParameterInfo()
    p = _by_name(params)
    p['input'].value = input_path
    p['output'].value = output_path
    p['output_format'].value = output_format
    p['source_datum'].value = source
    p['target_datum'].value = target
    return tool, params, p


def test_execute_messages_for_a_run_stopped_while_planning(monkeypatch, tmp_dir):
    toolbox, fake = _load_toolbox(monkeypatch)
    calls = []

    def fake_run_batch(*args, **kwargs):
        calls.append(kwargs)
        return fake_run_batch.result

    monkeypatch.setattr(toolbox.EGMTrans, 'run_batch', fake_run_batch)
    monkeypatch.setattr('egmtrans.download.ensure_grids', lambda **kwargs: calls.append(('grids', kwargs)) or [])
    tiles = os.path.join(tmp_dir, 'tiles')
    os.makedirs(tiles)
    tool, params, p = _prepare_tool(
        toolbox, tmp_dir, input_path=tiles, output_path=os.path.join(tmp_dir, 'out'), output_format='DTED2',
        source='EGM2008',
    )
    p['skip_existing'].value = True

    fake_run_batch.result = BatchResult(exit_code=1)
    tool.execute(params, _Messages())
    texts = [text for _kind, text in fake.messages]
    assert any('stopped before writing' in text for text in texts)
    assert not any('0 DEM(s)' in text for text in texts)
    assert texts[-1] == 'Processing stopped with errors.'
    grid_calls = [kwargs for kind, kwargs in [c for c in calls if isinstance(c, tuple)]]
    assert grid_calls and grid_calls[0]['filenames'] == ['us_nga_egm08_1.tif', 'us_nga_egm96_1.tif']
    assert 'datums_dir' in grid_calls[0]
    batch_kwargs = [c for c in calls if isinstance(c, dict)][0]
    assert batch_kwargs['dted_level'] == 2 and batch_kwargs['dted_naming'] == 'dted'
    assert batch_kwargs['skip_existing'] is True
    fake.env.isCancelled = True
    assert batch_kwargs['should_stop']() is True

    fake.messages.clear()
    fake_run_batch.result = BatchResult(exit_code=1, failed=[('a', 'x'), ('b', 'y')])
    tool.execute(params, _Messages())
    texts = [text for _kind, text in fake.messages]
    assert any('2 DEM(s) were not transformed' in text for text in texts)

    fake.messages.clear()
    fake_run_batch.result = BatchResult(exit_code=1, cancelled=True)
    tool.execute(params, _Messages())
    texts = [text for _kind, text in fake.messages]
    assert any('cancelled' in text for text in texts)

    fake.messages.clear()
    fake_run_batch.result = BatchResult(exit_code=0, files_processed=3)
    tool.execute(params, _Messages())
    texts = [text for _kind, text in fake.messages]
    assert texts[-1] == 'Processing completed.'

    for fmt, level in (('DTED1', 1), ('DTED0', 0), ('GeoTIFF', None)):
        p['output_format'].value = fmt
        calls.clear()
        tool.execute(params, _Messages())
        batch_kwargs = [c for c in calls if isinstance(c, dict)][0]
        assert batch_kwargs['dted_level'] == level, fmt


@requires_grids
def test_execute_writes_the_standard_tree_and_adds_no_layer(monkeypatch, tmp_dir):
    from egmtrans.dted.selftest import FINE, LAT0_SOUTH, LON0, northern_tile, southern_tile, write_tile

    toolbox, fake = _load_toolbox(monkeypatch)
    monkeypatch.setattr('egmtrans.download.ensure_grids', lambda **kwargs: [])
    tiles = os.path.join(tmp_dir, 'tiles')
    os.makedirs(tiles)
    write_tile(os.path.join(tiles, 'south.tif'), southern_tile(northern_tile()), LON0, LAT0_SOUTH, FINE, FINE)
    out = os.path.join(tmp_dir, 'out')
    tool, params, p = _prepare_tool(
        toolbox, tmp_dir, input_path=tiles, output_path=out, output_format='DTED2', source='EGM96',
    )
    p['dted_profile'].value = PROFILE
    p['min_patch_size'].value = 400
    messages = _Messages()
    tool.execute(params, messages)
    texts = [text for _kind, text in fake.messages]
    assert os.path.isfile(os.path.join(out, 'DTED', 'E006', 'N49.dt2')), texts
    assert not os.path.exists(os.path.join(out, 'DTED', 'E006', 'N49.dt2.aux.xml'))
    assert fake.management.calls == [] and fake.set_parameters == []
    assert any('not added to the map' in line for line in messages.lines)
    assert texts[-1] == 'Processing completed.'
    assert any('Build DMED' in text for text in texts)


def test_the_version_on_disk_is_read_as_text(monkeypatch):
    import egmtrans

    toolbox, _fake = _load_toolbox(monkeypatch)
    assert toolbox._version_on_disk() == egmtrans.__version__
    with open(TOOLBOX, encoding='utf-8') as handle:
        assert 'exec(' not in handle.read()


def test_display_names_and_help_match_the_xml_files(monkeypatch):
    toolbox, _fake = _load_toolbox(monkeypatch)
    for tool_class in toolbox.Toolbox().tools:
        xml_path = os.path.join(ROOT, 'arcgis', f'EGMTransToolbox.{tool_class.__name__}.pyt.xml')
        assert os.path.isfile(xml_path), xml_path
        root = ET.parse(xml_path).getroot()
        tool_element = root.find('tool')
        assert tool_element is not None and tool_element.get('displayname') == tool_class().label
        documented = {param.get('name'): param for param in tool_element.iter('param')}
        for parameter in tool_class().getParameterInfo():
            if parameter.direction == 'Output' and parameter.parameterType == 'Derived':
                continue
            assert parameter.name in documented, f'{tool_class.__name__}: {parameter.name} has no help'
            element = documented[parameter.name]
            assert element.get('displayname') == parameter.displayName, (tool_class.__name__, parameter.name)
            help_text = element.findtext('dialogReference') or ''
            assert re.sub(r'<[^>]+>', '', help_text).strip(), f'{tool_class.__name__}: {parameter.name} has empty help'
        for name in documented:
            assert name in {param.name for param in tool_class().getParameterInfo()}, (tool_class.__name__, name)


def test_the_dmed_tool(monkeypatch, tmp_dir):
    toolbox, fake = _load_toolbox(monkeypatch)
    tool = toolbox.DtedDmed()
    params = tool.getParameterInfo()
    assert [param.name for param in params] == ['folder', 'dmed_file', 'check_only']
    p = _by_name(params)
    delivery = os.path.join(tmp_dir, 'delivery')
    os.makedirs(os.path.join(delivery, 'DTED', 'E006'))
    _write_dted0(os.path.join(delivery, 'DTED', 'E006', 'N49.dt0'), 6, 49)
    empty = os.path.join(tmp_dir, 'empty')
    os.makedirs(empty)

    p['folder'].value = empty
    _validate(tool, params)
    assert any('no DTED folder' in text for text in _errors(p['folder']))
    p['folder'].value = delivery
    _validate(tool, params)
    assert not p['folder'].messages

    tool.execute(params, _Messages())
    texts = [text for _kind, text in fake.messages]
    dmed = os.path.join(delivery, 'DMED')
    assert os.path.isfile(dmed) and os.path.getsize(dmed) == 2 * 394, texts
    assert any('Wrote' in text and '2 records' in text for text in texts)

    fake.messages.clear()
    p['check_only'].value = True
    tool.execute(params, _Messages())
    assert any('matches the cells' in text for _kind, text in fake.messages)
