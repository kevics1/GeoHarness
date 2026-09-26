"""Native cartography package — pure-Python map rendering (no QGIS).

Public API::

    from geoharness.cartography import (
        render_map, render_static, render_interactive, render_layers,
    )

* ``render_static``      → PNG / PDF / SVG via matplotlib (Agg backend)
* ``render_interactive`` → HTML via folium (embedded Leaflet)
* ``render_map``         → dispatch on the output suffix
* ``render_layers``      → compose several styled layers onto ONE map

Importing this package is headless-safe: the Agg backend is forced before
``pyplot`` is touched, so it never opens a GUI window.
"""

from __future__ import annotations

from geoharness.cartography.renderer import (
    render_interactive,
    render_layers,
    render_map,
    render_static,
)

__all__ = [
    "render_interactive",
    "render_layers",
    "render_map",
    "render_static",
]
