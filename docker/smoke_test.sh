#!/usr/bin/env bash
# Prove the EGMTrans image transforms a GeoTIFF and a DTED file, makes a standard
# DTED delivery from GeoTIFF tiles with its DMED, and refuses an EGM2008 DTED
# target, with the network off. Every input is synthetic: the self-test's tiles.
#
#   docker/smoke_test.sh                   build egmtrans:smoke, then test it
#   docker/smoke_test.sh egmtrans:1.9.0    test an image that is already built
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
image="${1:-}"
if [[ -z "$image" ]]; then
    image=egmtrans:smoke
    docker build -t "$image" "$root"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 0777 "$work"

# egmtrans, and python, in the image: as the calling user, with no network.
run() { docker run --rm --network none --user "$(id -u):$(id -g)" -v "$work:/data" "$image" "$@"; }
py() { docker run --rm --network none --user "$(id -u):$(id -g)" -v "$work:/data" --entrypoint python "$image" -c "$1"; }

echo "0/5 The self-test writes its synthetic tiles and EGM96 cells"
run dted-selftest --keep /data/selftest >/dev/null 2>&1 || { echo "    the self-test did not reproduce the reference bytes" >&2; exit 1; }
mkdir -p "$work/tiles"
cp "$work/selftest/south.tif" "$work/selftest/north.tif" "$work/tiles/"

echo "1/5 GeoTIFF: a synthetic tile (EGM2008) to EGM96"
run -i /data/tiles/south.tif -o /data/south_egm96.tif -s EGM2008 -t EGM96 -y -l False >/dev/null
py "
import numpy as np
from osgeo import gdal
src = gdal.Open('/data/tiles/south.tif')
source = src.GetRasterBand(1).ReadAsArray()
void = source == src.GetRasterBand(1).GetNoDataValue()
ds = gdal.Open('/data/south_egm96.tif')
out = ds.ReadAsArray()
wkt = ds.GetSpatialRef().ExportToWkt()
assert 'EGM96' in wkt, wkt
assert np.array_equal(void, np.isnan(out)), f'{int(np.isnan(out).sum())} voids in the output, {int(void.sum())} in the source'
ocean = np.abs(source) < 0.015
assert np.all(out[ocean & ~void] == 0), 'ocean left 0 m'
print(f'    compound CRS carries EGM96 height, {int(ocean.sum()):,} ocean posts at 0 m, voids kept: ok')
"

echo "2/5 DTED2: a cell relabeled E08 to EGM96 (the DTED-to-DTED path)"
py "
from egmtrans.dted.header import read_header, write_header
path = '/data/selftest/N49E006.dt2'
header = read_header(path)
header.set_raw('dsi.vertical_datum', 'E08')
write_header(path, header)
"
run -i /data/selftest/N49E006.dt2 -o /data/N49E006_egm96.dt2 -s EGM2008 -t EGM96 -y -l False >/dev/null
py "
import numpy as np
from osgeo import gdal
messages = []
gdal.PushErrorHandler(lambda cls, no, msg: messages.append(msg))
gdal.SetConfigOption('DTED_VERIFY_CHECKSUM', 'YES')
src = gdal.Open('/data/selftest/N49E006.dt2').ReadAsArray().astype(int)
out = gdal.Open('/data/N49E006_egm96.dt2').ReadAsArray().astype(int)
gdal.PopErrorHandler()
with open('/data/N49E006_egm96.dt2', 'rb') as f:
    code = f.read()[221:224].decode()
void = src == -32767
assert not [m for m in messages if 'checksum' in m.lower()], messages
assert code == 'E96', code
assert np.array_equal(void, out == -32767), 'voids moved'
assert np.all(out[src == 0] == 0), 'ocean left 0 m'
shift = out[~void] - src[~void]
assert shift.min() >= -2 and shift.max() <= 2, 'the datum shift is not of the expected size'
print(f'    header E96, {int(void.sum()):,} voids kept, ocean at 0 m, record checksums valid: ok')
"

echo "3/5 Unattended: a prompt that cannot be answered exits with code 2"
set +e
run -i /data/selftest/N49E006.dt2 -o /data/mismatch.dt2 -s EGM96 -t EGM96 -l False >/dev/null 2>&1
rc=$?
set -e
if [[ $rc -ne 2 ]]; then
    echo "    expected exit code 2 without --yes, got $rc" >&2
    exit 1
fi
echo "    header says E08 but -s says EGM96, no --yes: exit code 2: ok"

echo "4/5 DTED from GeoTIFF: two tiles to the standard tree, headers checked, DMED built"
cat > "$work/product.toml" <<'TOML'
schema = 1
[product]
dted_level = 2
security_code = "U"
data_edition = 1
match_merge_version = "A"
producer_code = "USNGA"
digitizing_system = "SYNTHETIC"
compilation_date = "2026-01"
abs_horiz_acc = 10
abs_vert_acc = 5
rel_horiz_acc = "NA"
rel_vert_acc = 3
TOML
run -i /data/tiles -o /data/delivery -s EGM2008 -t EGM96 -y -l False -m True -p 400 --dted-level 2 \
    --dted-profile /data/product.toml >/dev/null
for cell in E006/N49 E006/N50; do
    [[ -f "$work/delivery/DTED/$cell.dt2" ]] || { echo "    $cell.dt2 is missing from the tree" >&2; exit 1; }
done
[[ -f "$work/delivery/DTED/E006/south_mask.tif" && -f "$work/delivery/DTED/E006/north_mask.tif" ]] \
    || { echo "    the masks are not beside their cells" >&2; exit 1; }
run dted-header /data/delivery/DTED/E006/N49.dt2 /data/delivery/DTED/E006/N50.dt2 --check-data >/dev/null
run dmed /data/delivery >/dev/null 2>&1
py "
import os
size = os.path.getsize('/data/delivery/DMED')
assert size == 3 * 394, size
with open('/data/delivery/DMED', 'rb') as f:
    data = f.read()
assert data[:14] == b'N49N51E006E007', data[:14]
assert data[394:401] == b'N49E006' and data[788:795] == b'N50E006'
print('    DTED/E006/N49.dt2 and N50.dt2 with masks beside them, headers and records clean, DMED of 3 records: ok')
"
run dmed /data/delivery --check >/dev/null 2>&1

echo "5/5 An EGM2008 DTED target is refused before anything runs"
set +e
run -i /data/tiles -o /data/refused -s EGM96 -t EGM2008 -y -l False --dted-level 2 --dted-profile /data/product.toml >/dev/null 2>&1
rc=$?
set -e
if [[ $rc -ne 2 || -e "$work/refused" ]]; then
    echo "    expected exit code 2 and nothing written, got $rc" >&2
    exit 1
fi
echo "    DTED is written in EGM96 only: exit code 2, nothing written: ok"

echo "Smoke test passed: $image"
