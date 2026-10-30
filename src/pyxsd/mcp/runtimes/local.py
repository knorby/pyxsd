"""Uncontained local clean-worker runtime; never an execution sandbox."""

from ..workers import WorkerPool


class LocalRuntime(WorkerPool):
    """The same lifecycle/protocol boundary without OS containment claims."""
