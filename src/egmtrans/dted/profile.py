"""The product profile: a TOML file holding the header constants of one DTED
product (security markings, producer, digitizing system, free text, ...) and
the mappings the index builder uses to harvest per-tile values from raster
tags and XML sidecars.

    schema = 1

    [product]
    dted_level = 2
    security_code = "U"
    producer_code = "USNGA"
    digitizing_system = "TandemXTDF"
    abs_horiz_acc = 14
    rel_horiz_acc = "NA"

    [harvest.xml]
    sidecar = "{stem}.xml"
    [harvest.xml.namespaces]
    gmd = "http://www.isotc211.org/2005/gmd"
    [harvest.xml.fields]
    compilation_date = "//gmd:dateStamp/gco:Date"

    [harvest.tags.fields]
    source_version = { tag = "TIFFTAG_SOFTWARE", pattern = "DEMES ([0-9.]+)" }

Keys of ``[product]`` are index columns (see :mod:`egmtrans.dted.index`); a
per-cell value in the index overrides the profile constant.
"""

from __future__ import annotations

import datetime as dt
import os
import tomllib
from dataclasses import dataclass, field

from egmtrans.dted.index import COLUMNS_BY_NAME, Column, check_value

PROFILE_SCHEMA_VERSION = 1


@dataclass
class HarvestConfig:
    """Where the index builder finds per-tile values."""

    xml_sidecar: str | None = None
    xml_namespaces: dict[str, str] = field(default_factory=dict)
    xml_fields: dict[str, dict] = field(default_factory=dict)
    tag_fields: dict[str, dict] = field(default_factory=dict)


@dataclass
class Profile:
    """A loaded profile: header constants keyed by index column, and the harvest mappings."""

    path: str
    product: dict[str, object]
    harvest: HarvestConfig
    schema_version: int = PROFILE_SCHEMA_VERSION

    @property
    def level(self) -> int | None:
        value = self.product.get('dted_level')
        return int(value) if value is not None else None

    def get(self, column: str, default=None):
        return self.product.get(column, default)


def _mapping(column: str, spec, kind: str) -> dict:
    """Normalize a harvest mapping: a bare string, or a table with the source and an optional pattern."""
    source_key = 'xpath' if kind == 'xml' else 'tag'
    if isinstance(spec, str):
        return {source_key: spec, 'pattern': None}
    if isinstance(spec, dict) and source_key in spec:
        extra = set(spec) - {source_key, 'pattern'}
        if extra:
            raise ValueError(f'harvest.{kind}.fields.{column}: unknown keys {sorted(extra)}')
        return {source_key: spec[source_key], 'pattern': spec.get('pattern')}
    raise ValueError(f'harvest.{kind}.fields.{column} must be a string or a table with "{source_key}"')


def validate_product(product: dict) -> list[str]:
    """Problems with the ``[product]`` table: unknown or non-header keys, bad values."""
    problems = []
    for column_name, value in product.items():
        column: Column | None = COLUMNS_BY_NAME.get(column_name)
        if column is None:
            problems.append(f'product.{column_name}: not an index column')
            continue
        if column.dted_key is None and column_name != 'dted_level':
            problems.append(f'product.{column_name}: not a header field, so it has no place in a profile')
            continue
        if isinstance(value, dt.datetime):
            value = value.date()
        problem = check_value(column, value)
        if problem:
            problems.append(f'product.{column_name}: {problem}')
    return problems


def load_profile(path: str) -> Profile:
    """Read and check a profile file.

    Raises:
        ValueError: If the file is not valid TOML or holds unknown or malformed entries.
    """
    try:
        with open(path, 'rb') as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f'{os.path.basename(path)} is not valid TOML: {e}') from e
    except OSError as e:
        raise ValueError(f'Cannot read profile {path}: {e}') from e

    unknown = set(document) - {'schema', 'product', 'harvest'}
    if unknown:
        raise ValueError(f'{os.path.basename(path)}: unknown top-level keys {sorted(unknown)}')
    schema_version = int(document.get('schema', PROFILE_SCHEMA_VERSION))
    if schema_version != PROFILE_SCHEMA_VERSION:
        raise ValueError(f'{os.path.basename(path)}: profile schema {schema_version} is not supported')

    product = dict(document.get('product', {}))
    for key, value in list(product.items()):
        if isinstance(value, dt.datetime):
            product[key] = value.date()
    problems = validate_product(product)
    if problems:
        raise ValueError(f'{os.path.basename(path)}:\n  ' + '\n  '.join(problems))

    harvest_doc = document.get('harvest', {})
    xml_doc = harvest_doc.get('xml', {})
    tags_doc = harvest_doc.get('tags', {})
    harvest = HarvestConfig(
        xml_sidecar=xml_doc.get('sidecar'),
        xml_namespaces=dict(xml_doc.get('namespaces', {})),
        xml_fields={column: _mapping(column, spec, 'xml') for column, spec in xml_doc.get('fields', {}).items()},
        tag_fields={column: _mapping(column, spec, 'tags') for column, spec in tags_doc.get('fields', {}).items()},
    )
    for column in list(harvest.xml_fields) + list(harvest.tag_fields):
        if column not in COLUMNS_BY_NAME:
            raise ValueError(f'{os.path.basename(path)}: harvest field {column!r} is not an index column')
    return Profile(path=path, product=product, harvest=harvest, schema_version=schema_version)
