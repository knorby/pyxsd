"""W3C XML Schema Test Suite integration.

This package runs the pinned ``w3c/xsdtests`` corpus against pyxsd and an
independent ``xmlschema`` oracle.  It is test infrastructure: it does not
change the library under test.

Check the corpus out with::

    git submodule update --init tests/xsts/corpus

then run a local report with::

    python tests/report_xsts.py --profile xsd11 --limit 500

The corpus is optional: the rest of the test suite runs without it.
"""
