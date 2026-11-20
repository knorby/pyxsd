<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
  <xsl:function name="f" xmlns:xs="http://www.w3.org/2001/XMLSchema" as="xs:integer">
    <xsl:param name="arg" as="xs:integer"/>
    <xsl:sequence select="$arg"/>
  </xsl:function>
</xsl:stylesheet>
