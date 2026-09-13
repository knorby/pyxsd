"""Build and execute test cases from a parsed catalogue.

A :class:`Case` is one schema or instance test together with the schema
documents it depends on.  Its expectation is selected for the active profile;
a test the profile does not claim is still enumerated, as
``NOT_APPLICABLE``, so nothing disappears silently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .catalog import Catalog, DocumentRef, load_catalog
from .drivers import HarnessError, PyXSDDriver, XmlSchemaDriver, build_bundle
from .outcomes import EngineResult, Outcome, classify
from .selection import MetadataError, Profile, expected_is_valid, select_expected

#: Location of the pinned corpus submodule.
CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"


def corpus_available() -> bool:
    """Whether the test-suite submodule is checked out."""
    return (CORPUS_ROOT / "suite.xml").is_file()


def load_default_catalog() -> Catalog:
    """Parse the pinned corpus under :data:`CORPUS_ROOT`."""
    return load_catalog(PurePosixPath(str(CORPUS_ROOT)))


SCHEMA = "schema"
INSTANCE = "instance"


@dataclass(frozen=True)
class Case:
    """One runnable (or deliberately non-runnable) test."""

    test_id: str
    set_name: str
    contributor: str
    group_name: str
    kind: str
    applicable: bool
    schema_documents: tuple[DocumentRef, ...]
    corpus_root: PurePosixPath
    expected_validity: str | None = None
    instance_ref: DocumentRef | None = None
    metadata_error: str | None = None
    version_tokens: tuple[str, ...] = ()

    @property
    def expected_valid(self) -> bool | None:
        if self.expected_validity is None:
            return None
        return expected_is_valid(self.expected_validity)


@dataclass
class CaseResult:
    """The outcome of one case under one engine."""

    test_id: str
    set_name: str
    contributor: str
    group_name: str
    kind: str
    engine: str
    outcome: Outcome
    expected: str | None = None
    actual: bool | None = None
    detail: str | None = None

    @property
    def key(self) -> str:
        return f"{self.engine}:{self.test_id}:{self.kind}"


def _flatten(
    catalog: Catalog,
    profile: Profile,
) -> Iterator[Case]:
    root = catalog.root
    for test_set in catalog.suite.test_sets:
        set_ok = profile.supports(test_set.version)
        for group in test_set.groups:
            group_ok = set_ok and profile.supports(group.version)
            yield from _schema_case(root, profile, test_set, group, group_ok)
            yield from _instance_cases(root, profile, test_set, group, group_ok)


def _schema_case(
    root: PurePosixPath,
    profile: Profile,
    test_set: object,
    group: object,
    group_ok: bool,
) -> Iterator[Case]:
    schema_test = group.schema_test  # type: ignore[attr-defined]
    if schema_test is None:
        return
    applicable = group_ok and profile.supports(schema_test.version)
    expected: str | None = None
    error: str | None = None
    try:
        selected = select_expected(schema_test.expected, profile)
    except MetadataError as exc:
        error = str(exc)
    else:
        if selected is None:
            applicable = False
        else:
            expected = selected.validity
    yield Case(
        test_id=f"{test_set.name}/{group.name}/{schema_test.name}",  # type: ignore[attr-defined]
        set_name=test_set.name,  # type: ignore[attr-defined]
        contributor=test_set.contributor,  # type: ignore[attr-defined]
        group_name=group.name,  # type: ignore[attr-defined]
        kind=SCHEMA,
        applicable=applicable,
        schema_documents=schema_test.documents,
        corpus_root=root,
        expected_validity=expected,
        metadata_error=error,
        version_tokens=schema_test.version,
    )


def _instance_cases(
    root: PurePosixPath,
    profile: Profile,
    test_set: object,
    group: object,
    group_ok: bool,
) -> Iterator[Case]:
    schema_test = group.schema_test  # type: ignore[attr-defined]
    documents = schema_test.documents if schema_test is not None else ()
    for instance_test in group.instance_tests:  # type: ignore[attr-defined]
        applicable = group_ok and profile.supports(instance_test.version)
        expected: str | None = None
        error: str | None = None
        try:
            selected = select_expected(instance_test.expected, profile)
        except MetadataError as exc:
            error = str(exc)
        else:
            if selected is None:
                applicable = False
            else:
                expected = selected.validity
        yield Case(
            test_id=f"{test_set.name}/{group.name}/{instance_test.name}",  # type: ignore[attr-defined]
            set_name=test_set.name,  # type: ignore[attr-defined]
            contributor=test_set.contributor,  # type: ignore[attr-defined]
            group_name=group.name,  # type: ignore[attr-defined]
            kind=INSTANCE,
            applicable=applicable,
            schema_documents=documents,
            corpus_root=root,
            expected_validity=expected,
            instance_ref=instance_test.document,
            metadata_error=error,
            version_tokens=instance_test.version,
        )


def build_cases(catalog: Catalog, profile: Profile) -> list[Case]:
    """Every schema and instance test in *catalog*, applicable or not."""
    return list(_flatten(catalog, profile))


@dataclass
class Runner:
    """Execute cases with one or more engines in a shared scratch directory."""

    profile: Profile
    driver: object
    oracle: object | None = None
    workdir: Path = field(default_factory=lambda: Path("."))

    def _bundle(self, case: Case) -> Path:
        return build_bundle(case.corpus_root, case.schema_documents, self.workdir)

    def _run_engine(
        self,
        engine: object,
        case: Case,
        bundle: Path | None,
        bundle_error: str | None = None,
    ) -> CaseResult:
        engine_name = getattr(engine, "name", type(engine).__name__)
        if case.metadata_error is not None:
            return CaseResult(
                test_id=case.test_id,
                set_name=case.set_name,
                contributor=case.contributor,
                group_name=case.group_name,
                kind=case.kind,
                engine=engine_name,
                outcome=Outcome.METADATA_ERROR,
                expected=case.expected_validity,
                detail=case.metadata_error,
            )
        if not case.applicable:
            return CaseResult(
                test_id=case.test_id,
                set_name=case.set_name,
                contributor=case.contributor,
                group_name=case.group_name,
                kind=case.kind,
                engine=engine_name,
                outcome=Outcome.NOT_APPLICABLE,
                expected=case.expected_validity,
                detail="version tokens not claimed by the profile",
            )
        if not case.schema_documents:
            return CaseResult(
                test_id=case.test_id,
                set_name=case.set_name,
                contributor=case.contributor,
                group_name=case.group_name,
                kind=case.kind,
                engine=engine_name,
                outcome=Outcome.ADAPTER_GAP,
                expected=case.expected_validity,
                detail="group has no schema; built-in components only",
            )
        if bundle_error is not None:
            return CaseResult(
                test_id=case.test_id,
                set_name=case.set_name,
                contributor=case.contributor,
                group_name=case.group_name,
                kind=case.kind,
                engine=engine_name,
                outcome=Outcome.ADAPTER_GAP,
                expected=case.expected_validity,
                detail=bundle_error,
            )
        try:
            if case.kind == SCHEMA:
                assert bundle is not None
                result: EngineResult = engine.compile_schema(bundle)  # type: ignore[attr-defined]
            else:
                assert case.instance_ref is not None
                assert bundle is not None
                instance = Path(case.corpus_root) / case.instance_ref.path
                if not instance.is_file():
                    return CaseResult(
                        test_id=case.test_id,
                        set_name=case.set_name,
                        contributor=case.contributor,
                        group_name=case.group_name,
                        kind=case.kind,
                        engine=engine_name,
                        outcome=Outcome.ERROR,
                        expected=case.expected_validity,
                        detail=f"instance document is missing: {instance}",
                    )
                result = engine.validate(bundle, instance)  # type: ignore[attr-defined]
        except HarnessError as exc:
            return CaseResult(
                test_id=case.test_id,
                set_name=case.set_name,
                contributor=case.contributor,
                group_name=case.group_name,
                kind=case.kind,
                engine=engine_name,
                outcome=Outcome.ADAPTER_GAP,
                expected=case.expected_validity,
                detail=str(exc),
            )
        except Exception as exc:
            return CaseResult(
                test_id=case.test_id,
                set_name=case.set_name,
                contributor=case.contributor,
                group_name=case.group_name,
                kind=case.kind,
                engine=engine_name,
                outcome=Outcome.ERROR,
                expected=case.expected_validity,
                detail=f"{type(exc).__name__}: {exc}",
            )

        actual = result.schema_valid if case.kind == SCHEMA else result.instance_valid
        outcome = classify(
            case.expected_valid,
            actual,
            applicable=True,
            engine=result,
        )
        detail = result.adapter_gap or result.error
        if detail is None:
            detail = result.schema_error if case.kind == SCHEMA else result.instance_error
        return CaseResult(
            test_id=case.test_id,
            set_name=case.set_name,
            contributor=case.contributor,
            group_name=case.group_name,
            kind=case.kind,
            engine=engine_name,
            outcome=outcome,
            expected=case.expected_validity,
            actual=actual,
            detail=detail,
        )

    def run_case(self, case: Case) -> list[CaseResult]:
        """Run one case through the primary driver and, if present, the oracle."""
        bundle: Path | None = None
        bundle_error: str | None = None
        if case.metadata_error is None and case.applicable and case.schema_documents:
            try:
                bundle = self._bundle(case)
            except HarnessError as exc:
                bundle_error = str(exc)
        results = [self._run_engine(self.driver, case, bundle, bundle_error)]
        if self.oracle is not None:
            results.append(self._run_engine(self.oracle, case, bundle, bundle_error))
        return results

    def run(
        self,
        cases: Iterable[Case],
        on_result: Callable[[CaseResult], None] | None = None,
    ) -> list[CaseResult]:
        """Run every case, optionally streaming each result as it is produced."""
        results: list[CaseResult] = []
        for case in cases:
            for result in self.run_case(case):
                results.append(result)
                if on_result is not None:
                    on_result(result)
        return results


#: Cases per worker task when running in parallel.
BATCH_SIZE = 50


def configure_logging() -> None:
    """Silence per-case noise during aggregate runs."""
    import warnings

    logging.getLogger("pyxsd").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", module=r"xmlschema\.")
    warnings.filterwarnings("ignore", module=r"pyxsd\.")


def _run_batch(payload: tuple[str, int, list[Case], str, bool, float]) -> list[CaseResult]:
    """Worker entry point: run a batch of cases to completion."""
    from .selection import PROFILES

    temp_root, index, chunk, profile_name, oracle_enabled, timeout = payload
    configure_logging()
    workdir = Path(temp_root) / f"w{index}"
    workdir.mkdir(parents=True, exist_ok=True)
    profile = PROFILES[profile_name]
    driver = PyXSDDriver(timeout=timeout)
    oracle = XmlSchemaDriver(profile_name, timeout=timeout) if oracle_enabled else None
    runner = Runner(profile=profile, driver=driver, oracle=oracle, workdir=workdir)
    results: list[CaseResult] = []
    for case in chunk:
        results.extend(runner.run_case(case))
    return results


def run_parallel(
    cases: list[Case],
    profile_name: str,
    *,
    oracle_enabled: bool,
    timeout: float,
    jobs: int,
    temp_root: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[CaseResult]:
    """Run cases across *jobs* worker processes.

    Cases are batched to amortise process and pickling overhead.  Each task
    gets its own scratch directory, so multi-document driver schemas cannot
    collide between workers.
    """
    chunks = [cases[i : i + BATCH_SIZE] for i in range(0, len(cases), BATCH_SIZE)]
    payloads = [
        (str(temp_root), index, chunk, profile_name, oracle_enabled, timeout)
        for index, chunk in enumerate(chunks)
    ]
    results: list[CaseResult] = []
    completed = 0
    with ProcessPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = {executor.submit(_run_batch, payload): len(payload[2]) for payload in payloads}
        for future in as_completed(futures):
            results.extend(future.result())
            completed += futures[future]
            if on_progress is not None:
                on_progress(completed, len(cases))
    return results
