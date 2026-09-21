# EGMTrans container: vertical datum transformation of DTED and GeoTIFF files.
#
#   docker build -t egmtrans .
#   docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/data" egmtrans \
#       -i in.tif -o out.tif -s EGM2008 -t EGM96 -y
#
# The image carries the two 1-arc-minute geoid grids the transforms read, so it
# runs with no network access. See "Run in a container" in README.md.

ARG MINIFORGE_TAG=26.7.2-0
FROM condaforge/miniforge3:${MINIFORGE_TAG}

ENV EGMTRANS_BASE_PATH=/opt/egmtrans \
    NUMBA_CACHE_DIR=/opt/numba-cache \
    PROJ_DATA=/opt/conda/envs/egmtrans/share/proj \
    GDAL_DATA=/opt/conda/envs/egmtrans/share/gdal \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# GDAL comes from conda-forge: pip wheels for GDAL are not reliable.
RUN mamba create -y -n egmtrans -c conda-forge \
        python=3.11 "gdal>=3.11" "numpy>=1.22" "scipy>=1.7" "numba>=0.60" "tqdm>=4.60" \
        pip setuptools \
    && mamba clean -afy
ENV PATH=/opt/conda/envs/egmtrans/bin:$PATH

WORKDIR /opt/egmtrans

# Grids first, from the stdlib-only download module, so that code changes do not
# invalidate this ~730 MB layer. ensure_grids verifies each file's pinned SHA-256.
COPY src/egmtrans/download.py /tmp/download.py
RUN python -c "import importlib.util as u; s = u.spec_from_file_location('dl', '/tmp/download.py'); \
m = u.module_from_spec(s); s.loader.exec_module(m); \
m.ensure_grids(datums_dir='/opt/egmtrans/datums', filenames=['us_nga_egm96_1.tif', 'us_nga_egm08_1.tif'])" \
    && rm /tmp/download.py

COPY pyproject.toml README.md LICENSE CHANGELOG.md SECURITY.md ./
COPY src ./src
COPY crs ./crs
COPY samples ./samples
COPY benchmarks ./benchmarks
RUN pip install --no-deps --no-build-isolation --no-cache-dir -e .

# Compile the Numba kernels once (float and DTED paths) so a run does not pay the
# JIT cost. The cache is world-writable: a host CPU unlike the build machine's
# recompiles into it, whatever --user the container runs as.
RUN mkdir -p "$NUMBA_CACHE_DIR" /tmp/warmup \
    && egmtrans -i samples/Copernicus_DSM_COG_10_N06_00_E126_00_DEM.tif -o /tmp/warmup/cop.tif \
        -s EGM2008 -t EGM96 -y -l False \
    && egmtrans -i samples/03n008e_SRTM.dt2 -o /tmp/warmup/srtm.dt2 -s EGM96 -t EGM2008 -y -l False \
    && rm -rf /tmp/warmup \
    && chmod -R a+rwX "$NUMBA_CACHE_DIR"

RUN useradd --create-home --uid 10001 egmtrans
USER egmtrans
WORKDIR /data

ENTRYPOINT ["egmtrans"]
CMD ["--help"]
