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

# Every fastmath flag except 'nnan' and 'ninf'. Those two let LLVM assume no
# NaN or infinity exists, which deletes the np.isnan() tests that keep voids out
# of the ocean and flat-patch masks: under fastmath=True every void was labeled
# ocean and written as 0 m.
FASTMATH_FLAGS = frozenset({'nsz', 'arcp', 'contract', 'afn', 'reassoc'})

try:
    from numba import njit, prange
    NUMBA_AVAILABLE = True

    def get_numba_decorator(parallel=False, arc_safe=False):
        """Return appropriate Numba decorator based on configuration.

        Args:
            parallel: Whether to enable parallel execution.
            arc_safe: If True, never uses parallel mode (prevents ArcGIS Pro crashes).
        """
        # Numba accepts a set but not a frozenset, and keeps a reference to it.
        fastmath = set(FASTMATH_FLAGS)
        if arc_safe and _state.get_arc_mode():
            return njit(fastmath=fastmath, cache=NUMBA_CACHE)
        if parallel and PARALLEL_ENABLED:
            return njit(parallel=True, fastmath=fastmath, cache=NUMBA_CACHE)
        return njit(fastmath=fastmath, cache=NUMBA_CACHE)

except ImportError:
    NUMBA_AVAILABLE = False

    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if args and callable(args[0]) else decorator

    def prange(*args):
        return range(*args)

    def get_numba_decorator(parallel=False, arc_safe=False):
        """Return a dummy decorator when Numba is not available."""
        return lambda func: func
