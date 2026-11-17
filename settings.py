# -*- coding: utf-8 -*-
"""
settings.py - 插件设置持久化

存进 QgsSettings，跟着 QGIS 用户配置走，重装插件也不丢。
"""

from qgis.core import QgsSettings

PREFIX = 'point_bearing_tool'


class Settings(object):
    """读写插件设置"""

    def __init__(self):
        self.s = QgsSettings()

    def _key(self, name):
        return '%s/%s' % (PREFIX, name)

    def get(self, name, default=None):
        return self.s.value(self._key(name), default)

    def set(self, name, value):
        self.s.setValue(self._key(name), value)

    # ---------- DEM ----------
    @property
    def dem_layer_id(self):
        return self.get('dem_layer_id', '')

    @dem_layer_id.setter
    def dem_layer_id(self, value):
        self.set('dem_layer_id', value or '')

    @property
    def observer_height(self):
        try:
            return float(self.get('observer_height', 1.7))
        except (TypeError, ValueError):
            return 1.7

    @observer_height.setter
    def observer_height(self, value):
        self.set('observer_height', float(value))

    @property
    def target_height(self):
        try:
            return float(self.get('target_height', 0.0))
        except (TypeError, ValueError):
            return 0.0

    @target_height.setter
    def target_height(self, value):
        self.set('target_height', float(value))

    @property
    def tolerance(self):
        try:
            return float(self.get('tolerance', 0.02))
        except (TypeError, ValueError):
            return 0.02

    @tolerance.setter
    def tolerance(self, value):
        self.set('tolerance', float(value))
