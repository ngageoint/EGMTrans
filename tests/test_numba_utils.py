"""Tests for egmtrans.numba_utils."""

from egmtrans.numba_utils import (
    NUMBA_AVAILABLE,
    NUMBA_CACHE,
    PARALLEL_ENABLED,
    TQDM_AVAILABLE,
    get_numba_decorator,
    tqdm,
)


def test_tqdm_fallback_iterable():
    """tqdm should iterate correctly even when using the dummy class."""
    result = list(tqdm([1, 2, 3]))
    assert result == [1, 2, 3]


def test_tqdm_context_manager():
    """tqdm should work as a context manager."""
    with tqdm(total=10) as pbar:
        pbar.update(5)


def test_get_numba_decorator_returns_callable():
    dec = get_numba_decorator()
    assert callable(dec)


def test_get_numba_decorator_parallel():
    dec = get_numba_decorator(parallel=True)
    assert callable(dec)


def test_get_numba_decorator_arc_safe():
    dec = get_numba_decorator(arc_safe=True)
    assert callable(dec)


def test_default_decorator_takes_no_fast_math():
    """Fast-math lets the compiler reorder and fuse float operations, so a
    result could differ in the last place between hosts; only the spline
    kernels ask for it."""
    from egmtrans.numba_utils import FASTMATH_FLAGS

    if not NUMBA_AVAILABLE:
        return

    @get_numba_decorator()
    def strict(a, b):
        return a + b

    @get_numba_decorator(fastmath=True)
    def fast(a, b):
        return a + b

    assert not strict.targetoptions.get('fastmath')
    assert fast.targetoptions['fastmath'] == set(FASTMATH_FLAGS)
    assert strict(1.5, 2.25) == fast(1.5, 2.25) == 3.75


def test_decorator_preserves_function():
    """The decorator (with or without numba) should produce a callable."""
    dec = get_numba_decorator()

    @dec
    def add(a, b):
        return a + b

    # If numba is available, the decorated function is a numba dispatcher;
    # otherwise it's the original function. Either way it should be callable.
    assert callable(add)


def test_constants_are_booleans():
    assert isinstance(NUMBA_AVAILABLE, bool)
    assert isinstance(TQDM_AVAILABLE, bool)
    assert isinstance(PARALLEL_ENABLED, bool)
    assert isinstance(NUMBA_CACHE, bool)
