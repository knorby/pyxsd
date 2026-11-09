<xsl:stylesheet version="3.0"
                xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:template match="/">
    <result>
      <xsl:value-of select="/root/value"/>
    </result>
  </xsl:template>
</xsl:stylesheet>
