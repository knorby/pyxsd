{% extends "python/module.rst" %}

{% block content %}
{{ super() }}
{% if obj.id == "pyxsd" %}
Public exports
--------------

{% for name, kind in lazy_exports if name not in obj.children|map(attribute="short_name")|list %}
.. auto{{ kind }}:: pyxsd.{{ name }}
{% if kind == "class" %}
   :members:
   :undoc-members:
{% endif %}

{% endfor %}
{% endif %}
{% endblock %}
