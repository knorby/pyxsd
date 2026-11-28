"""Shared helpers for XSLT test modules.

The helpers preserve the small, explicit stylesheet and transformation shapes
used by the existing test families.  Namespace declarations are supplied by
the caller so a family keeps its own vocabulary without a second implementation
of the surrounding stylesheet wrapper.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
from collections.abc import Callable, Mapping
from concurrent.futures import Future, wait
from contextlib import suppress
from pathlib import Path
from threading import Barrier, Thread
from types import MappingProxyType
from typing import Any, TypeVar

from pyxsd.xdm.nodes import NodeKind, XdmDocument, XdmNode
from pyxsd.xslt.processor import XsltProcessor

XSLT_NS = "http://www.w3.org/1999/XSL/Transform"
XSD_NS = "http://www.w3.org/2001/XMLSchema"

_NO_NAMESPACES: Mapping[str, str] = MappingProxyType({})
_PROBE_CLEANUP_TIMEOUT = 5
_WORKER_TIMEOUT = 120.0
_T = TypeVar("_T")


def run_workers(
    barrier: Barrier, work: Callable[[int], _T], *, timeout: float = _WORKER_TIMEOUT
) -> list[_T]:
    """Run one worker per barrier party with a bounded join.

    A worker that deadlocks after the barrier cannot hold the test host open:
    the futures are awaited with *timeout*, then the barrier is aborted and the
    stuck workers are named.  Raw daemon threads are used because
    ``ThreadPoolExecutor`` joins its workers at interpreter shutdown, even after
    ``shutdown(wait=False)``.
    """
    errors: list[BaseException] = []
    futures: list[Future[_T]] = [Future() for _ in range(barrier.parties)]

    def guarded(index: int) -> None:
        try:
            result = work(index)
        except BaseException as error:
            errors.append(error)
            barrier.abort()
            futures[index].set_exception(error)
        else:
            futures[index].set_result(result)

    for index in range(barrier.parties):
        Thread(target=guarded, args=(index,), daemon=True).start()
    _, pending = wait(futures, timeout=timeout)
    if pending:
        barrier.abort()
        stuck = ", ".join(
            f"worker {index}" for index, future in enumerate(futures) if future in pending
        )
        raise AssertionError(f"timed out waiting for {stuck}")
    if errors:
        # Report the original worker failure, rather than a peer's broken barrier.
        raise errors[0]
    return [future.result(timeout=0) for future in futures]


def _kill_probe_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=_PROBE_CLEANUP_TIMEOUT,
        )
    else:
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


def run_probe(
    command: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a deadline-bounded probe and terminate its worker tree on any wait failure."""
    # Popen.__exit__ would wait without a deadline after a cleanup failure.
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        with suppress(BaseException):
            _kill_probe_tree(process.pid)
        try:
            process.communicate(timeout=_PROBE_CLEANUP_TIMEOUT)
        except BaseException:
            # A stuck drain must not replace the original failure or leave
            # this caller waiting for EOF from an inherited pipe.
            with suppress(BaseException):
                process.kill()
            with suppress(BaseException):
                process.stdout.close()
            with suppress(BaseException):
                process.stderr.close()
            with suppress(BaseException):
                process.wait(timeout=_PROBE_CLEANUP_TIMEOUT)
        raise
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    result.check_returncode()
    return result


def stylesheet(
    body: str,
    *,
    version: str = "3.0",
    attributes: str = "",
    namespaces: Mapping[str, str] = _NO_NAMESPACES,
    declare_xs: bool = True,
) -> bytes:
    """Wrap *body* in one XSLT stylesheet with the given declarations.

    The ``xs`` prefix is declared unless ``declare_xs=False``: literal result
    elements copy in-scope namespaces into their serialization, so a family
    whose output contract excludes the declaration must not have it silently
    added.
    """
    fixed = f' xmlns:xsl="{XSLT_NS}"'
    if declare_xs:
        fixed += f' xmlns:xs="{XSD_NS}"'
    declarations = "".join(f' xmlns:{prefix}="{uri}"' for prefix, uri in namespaces.items())
    return (
        f"<xsl:stylesheet{fixed}"
        f'{declarations} version="{version}"{attributes}>'
        f"{body}</xsl:stylesheet>"
    ).encode()


def transform(
    source: bytes,
    input_source: bytes | XdmDocument | None = None,
    *,
    delivery: str = "document",
    **options: Any,
) -> Any:
    """Compile *source* and run it with the test's delivery and options."""
    compiled = XsltProcessor().compile(io.BytesIO(source))
    if input_source is None:
        return compiled.transform(delivery=delivery, **options)
    return compiled.transform(source=input_source, delivery=delivery, **options)


def element_children(node: Any) -> tuple[XdmNode, ...]:
    """The element children of *node*, in document order."""
    return tuple(child for child in node.children if child.kind is NodeKind.ELEMENT)
