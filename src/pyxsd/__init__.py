"""pyxsd: schema-guided XML-to-Python object mapping with a transform pipeline."""

__version__ = "1.0.0.dev0"

__all__ = ["PyXSD", "__version__"]


def __getattr__(name: str):
    # Lazy import so that ``import pyxsd`` does not pull the parser stack
    # (and the ER tag registry) unless the main class is actually used.
    if name == "PyXSD":
        from pyxsd.parser import PyXSD

        return PyXSD
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
