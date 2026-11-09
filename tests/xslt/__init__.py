"""Independent W3C XSLT 3.0 test harness.

This package parses the pinned ``w3c/xslt30-test`` corpus and will grow the
selection, execution, assertion, baseline, and reporting layers.  It is test
infrastructure: catalog parsing never imports the XSLT engine under test.

Check the corpus out with::

    git submodule update --init tests/xslt/corpus

The corpus is optional for the rest of the test suite; catalog tests fall back
to small synthetic fixtures when it is absent.
"""
