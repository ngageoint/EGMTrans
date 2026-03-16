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
