# -*- coding: utf-8 -*-
"""plugin.py 的模块名与 QGIS 约定一致，这里只做入口导出"""


def classFactory(iface):  # noqa: N802
    """QGIS 插件入口"""
    from .plugin import PointBearingPlugin
    return PointBearingPlugin(iface)
