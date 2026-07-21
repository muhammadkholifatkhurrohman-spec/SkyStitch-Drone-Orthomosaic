"""
__init__.py
===========
Required entry point for a QGIS plugin. QGIS calls classFactory(iface)
to create the main plugin instance.
"""


def classFactory(iface):
    from .skystitch_plugin import SkyStitchPlugin

    return SkyStitchPlugin(iface)
