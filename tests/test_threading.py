"""Contract tests for the documented concurrency guarantees.

One ``Schema`` must not be parsed concurrently or reentrantly; concurrent
compiles and concurrent parses on separate ``Schema`` objects are supported.
These tests pin the supported half of that contract and are meaningful on
both GIL and free-threaded builds.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import fixture_dir
from pyxsd.schema import Schema


def run_threaded(func, *, num_threads=8, outer_iterations=5, pass_count=False):
    """Run *func* in many threads released together by a barrier."""
    for _ in range(outer_iterations):
        barrier = threading.Barrier(num_threads)
        with ThreadPoolExecutor(max_workers=num_threads) as pool:
            futures = [
                pool.submit(func, i, barrier) if pass_count else pool.submit(func, barrier)
                for i in range(num_threads)
            ]
            for future in futures:
                future.result()


def test_parallel_compiles_of_different_schemas():
    paths = [
        str(fixture_dir("inventory") / "schema.xsd"),
        str(fixture_dir("groups") / "schema.xsd"),
    ]

    def compile_one(i, barrier):
        barrier.wait()
        assert Schema.compile(paths[i % 2]) is not None

    run_threaded(compile_one, pass_count=True)


def test_parallel_parses_on_separate_schemas():
    directory = fixture_dir("inventory")
    expected = (
        Schema.compile(str(directory / "schema.xsd"))
        .parse(str(directory / "instance.xml"))
        .to_string()
    )

    def compile_and_parse(i, barrier):
        schema = Schema.compile(str(directory / "schema.xsd"))
        barrier.wait()
        assert schema.parse(str(directory / "instance.xml")).to_string() == expected

    run_threaded(compile_and_parse, pass_count=True)
