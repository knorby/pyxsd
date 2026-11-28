"""Installed-package smoke check for built pyxsd distributions.

The distribution tests install a wheel or source archive into an isolated
environment and run this script from outside the checkout::

    python distribution_smoke.py ENVIRONMENT-PREFIX CHECKOUT-ROOT

It fails when pyxsd comes from anywhere but the environment, when any loaded
module comes from the checkout, when the packaged XSLT schema resources are
unavailable through the public inspection path, or when a small deterministic
transformation serializes differently.  The script also exercises the two
packaged language profiles, whose schemas import the packaged XML Schema and
``xml.xsd`` resources.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

STYLESHEET = """\
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:output method="xml" omit-xml-declaration="yes"/>
  <xsl:template match="/catalog">
    <result count="{count(item)}">
      <xsl:for-each select="item[@keep = 'yes']">
        <entry><xsl:value-of select="@id"/></entry>
      </xsl:for-each>
    </result>
  </xsl:template>
</xsl:stylesheet>
"""

DOCUMENT = (
    '<catalog><item id="a" keep="yes"/><item id="b" keep="no"/><item id="c" keep="yes"/></catalog>'
)

EXPECTED = '<result count="3"><entry>a</entry><entry>c</entry></result>'


def leaked_modules(checkout: Path) -> list[str]:
    """Names of loaded modules whose files live inside *checkout*."""
    leaked = []
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        path = Path(origin).resolve()
        if path.is_relative_to(checkout):
            leaked.append(name)
    return sorted(leaked)


def main(argv: list[str]) -> int:
    """Run the installed-package smoke check, returning a process exit code."""
    if len(argv) != 3:
        print(
            "usage: distribution_smoke.py ENVIRONMENT-PREFIX CHECKOUT-ROOT",
            file=sys.stderr,
        )
        return 2
    environment = Path(argv[1]).resolve()
    checkout = Path(argv[2]).resolve()

    import pyxsd

    package_file = Path(pyxsd.__file__).resolve()
    if not package_file.is_relative_to(environment):
        print(f"pyxsd was imported from {package_file}, outside {environment}", file=sys.stderr)
        return 1
    leaked = leaked_modules(checkout)
    if leaked:
        print(f"modules imported from the checkout: {', '.join(leaked)}", file=sys.stderr)
        return 1

    from pyxsd.xslt import XsltProcessor, inspect_stylesheet

    for language in ("2.0", "3.0"):
        inspection = inspect_stylesheet(io.StringIO(STYLESHEET), language=language)
        if inspection.language != language:
            print(
                f"inspection selected {inspection.language!r}, not {language!r}",
                file=sys.stderr,
            )
            return 1

    compiled = XsltProcessor().compile(io.StringIO(STYLESHEET))
    result = compiled.transform(source=io.StringIO(DOCUMENT), delivery="stylesheet")
    serialized = result.serialize()
    if serialized != EXPECTED:
        print(f"serialized {serialized!r}, not {EXPECTED!r}", file=sys.stderr)
        return 1
    print(f"pyxsd {pyxsd.__version__} imported from {package_file}")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main(sys.argv))
