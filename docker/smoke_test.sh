#!/usr/bin/env bash
# Prove the EGMTrans image transforms a GeoTIFF and a DTED file with the network off.
#
#   docker/smoke_test.sh                  build egmtrans:smoke, then test it
#   docker/smoke_test.sh egmtrans:1.5.0   test an image that is already built
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

samples=/opt/egmtrans/samples

echo "1/3 GeoTIFF: Copernicus DEM (EGM2008) to EGM96"
run -i "$samples/Copernicus_DSM_COG_10_N06_00_E126_00_DEM.tif" -o /data/cop_egm96.tif \
    -s EGM2008 -t EGM96 -y -l False >/dev/null
py "
import numpy as np
from osgeo import gdal
src = gdal.Open('$samples/Copernicus_DSM_COG_10_N06_00_E126_00_DEM.tif').ReadAsArray()
ds = gdal.Open('/data/cop_egm96.tif')
out = ds.ReadAsArray()
wkt = ds.GetSpatialRef().ExportToWkt()
assert 'EGM96' in wkt, wkt
assert np.array_equal(np.isnan(src), np.isnan(out)), f'{int(np.isnan(out).sum())} voids in the output'
ocean = np.abs(np.round(src, 2)) < 0.01
assert np.all(out[ocean] == 0), 'ocean left 0 m'
print(f'    compound CRS carries EGM96 height, {int(ocean.sum()):,} ocean pixels at 0 m, no new voids: ok')
"

echo "2/3 DTED2: SRTM (EGM96) to EGM2008 and back"
run -i "$samples/03n008e_SRTM.dt2" -o /data/srtm_egm08.dt2 -s EGM96 -t EGM2008 -y -l False >/dev/null
run -i /data/srtm_egm08.dt2 -o /data/srtm_egm96.dt2 -s EGM2008 -t EGM96 -y -l False >/dev/null
py "
import numpy as np
from osgeo import gdal

messages = []
gdal.PushErrorHandler(lambda cls, no, msg: messages.append(msg))
gdal.SetConfigOption('DTED_VERIFY_CHECKSUM', 'YES')
src = gdal.Open('$samples/03n008e_SRTM.dt2').ReadAsArray().astype(int)
mid = gdal.Open('/data/srtm_egm08.dt2').ReadAsArray().astype(int)
out = gdal.Open('/data/srtm_egm96.dt2').ReadAsArray().astype(int)
gdal.PopErrorHandler()
with open('/data/srtm_egm96.dt2', 'rb') as f:
    code = f.read()[221:224].decode()
void = src == -32767
assert not [m for m in messages if 'checksum' in m.lower()], messages
assert code == 'E96', code
assert np.array_equal(void, out == -32767), 'voids moved'
assert np.all(out[src == 0] == 0), 'ocean left 0 m'
# Integer DTED cannot tell land that rounds to 0 m from ocean, so the return trip
# holds those posts at 0. Everything else must come back within DTED's 1 m rounding.
sea_level_land = ~void & (src != 0) & (mid == 0)
comparable = ~void & ~sea_level_land
assert np.abs(out[comparable] - src[comparable]).max() <= 1, 'round trip drifted more than DTED rounding'
print(f'    header E96, {int(void.sum()):,} voids kept, ocean at 0 m, record checksums valid,')
print(f'    round trip within 1 m except {int(sea_level_land.sum())} land posts that rounded to 0 m: ok')
"

echo "3/3 Unattended: a prompt that cannot be answered exits with code 2"
set +e
run -i "$samples/03n008e_SRTM.dt2" -o /data/mismatch.dt2 -s EGM2008 -t EGM96 -l False >/dev/null 2>&1
rc=$?
set -e
if [[ $rc -ne 2 ]]; then
    echo "    expected exit code 2 without --yes, got $rc" >&2
    exit 1
fi
echo "    header says EGM96 but -s says EGM2008, no --yes: exit code 2: ok"

echo "Smoke test passed: $image"
