<xsl:stylesheet xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="3.0">
   <xsl:template name="main">
      <out>
         <xsl:message>
            <smart>Smart element</smart>
         </xsl:message>
         <xsl:message>
            <xsl:comment>Chicken legs</xsl:comment>
         </xsl:message>
         <xsl:message select="'plain text'"/>
      </out>
   </xsl:template>
</xsl:stylesheet>
