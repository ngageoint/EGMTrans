"""Conditional numba/tqdm imports and decorator helpers."""

from egmtrans import _state

# ---------------------------------------------------------------------------
# tqdm
# ---------------------------------------------------------------------------
try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

    class tqdm:  # type: ignore[no-redef]
        """Dummy tqdm class for when tqdm is not installed."""

        def __init__(self, *args, **kwargs):
            self.iterable = args[0] if args else kwargs.get('iterable', None)

        def __iter__(self):
            if self.iterable:
                return iter(self.iterable)
            return iter([])

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def update(self, n=1):
            pass

# ---------------------------------------------------------------------------
# Numba
# ---------------------------------------------------------------------------
PARALLEL_ENABLED = True
NUMBA_CACHE = True

# The fast-math flags of the spline kernels, the only ones that still take any:
# every flag except 'nnan' and 'ninf'. Those two let LLVM assume no NaN or
# infinity exists, which deletes np.isnan() tests; under fastmath=True every void
# was labeled ocean and written as 0 m.
#
# No other kernel is compiled with fast-math. 'arcp', 'contract' and 'reassoc'
# let the compiler replace a division by a multiplication, fuse a multiply and
# an add, and reorder sums, so a result could differ by one unit in the last
# place between CPUs, LLVM versions and the plain-Python fallback; once heights
# are rounded to whole meters that is a 1 m difference at some post. Measured,
# the flags bought no time on the flat-area and bilinear kernels. Without them
# the spline results move by up to 3.7 mm, so those keep their flags, and no
# DTED output can use spline. Numba's on-disk cache is keyed on the kernel
# module's source, not on these options: change a kernel module whenever its
# options change, or a stale compilation is loaded.
FASTMATH_FLAGS = frozenset({'nsz', 'arcp', 'contract', 'afn', 'reassoc'})

try:
    from numba import njit, prange
    NUMBA_AVAILABLE = True

    def get_numba_decorator(parallel=False, arc_safe=False, fastmath=False):
        """Return appropriate Numba decorator based on configuration.

        Args:
            parallel: Whether to enable parallel execution.
            arc_safe: If True, never uses parallel mode (prevents ArcGIS Pro crashes).
            fastmath: Whether to compile with :data:`FASTMATH_FLAGS` (spline only).
        """
        options = {'cache': NUMBA_CACHE}
        if fastmath:
            # Numba accepts a set but not a frozenset, and keeps a reference to it.
            options['fastmath'] = set(FASTMATH_FLAGS)
        if parallel and PARALLEL_ENABLED and not (arc_safe and _state.get_arc_mode()):
            options['parallel'] = True
        return njit(**options)

except ImportError:
    NUMBA_AVAILABLE = False

    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if args and callable(args[0]) else decorator

    def prange(*args):
        return range(*args)

    def get_numba_decorator(parallel=False, arc_safe=False, fastmath=False):
        """Return a dummy decorator when Numba is not available."""
        return lambda func: func
