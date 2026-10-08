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
        python=3.14 "gdal>=3.13" "numpy>=2.0" "scipy>=1.15" "numba>=0.66" "tqdm>=4.60" pyarrow lxml \
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
COPY benchmarks ./benchmarks
RUN pip install --no-deps --no-build-isolation --no-cache-dir -e .

# Compile the Numba kernels once (the float path on a synthetic tile, the DTED
# path on a cell the self-test wrote) so a run does not pay the JIT cost. The
# cache is world-writable: a host CPU unlike the build machine's recompiles
# into it, whatever --user the container runs as.
RUN mkdir -p "$NUMBA_CACHE_DIR" /tmp/warmup \
    && egmtrans dted-selftest --keep /tmp/warmup \
    && egmtrans -i /tmp/warmup/south.tif -o /tmp/warmup/south_egm96.tif -s EGM2008 -t EGM96 -y -l False \
    && python -c "from egmtrans.dted.header import read_header, write_header; \
p = '/tmp/warmup/N49E006.dt2'; h = read_header(p); h.set_raw('dsi.vertical_datum', 'E08'); write_header(p, h)" \
    && egmtrans -i /tmp/warmup/N49E006.dt2 -o /tmp/warmup/N49E006_egm96.dt2 -s EGM2008 -t EGM96 -y -l False \
    && rm -rf /tmp/warmup \
    && chmod -R a+rwX "$NUMBA_CACHE_DIR"

RUN useradd --create-home --uid 10001 egmtrans
USER egmtrans
WORKDIR /data

ENTRYPOINT ["egmtrans"]
CMD ["--help"]
