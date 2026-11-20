# -*- coding: utf-8 -*-
"""
settings.py - 插件设置持久化

存进 QgsSettings，跟着 QGIS 用户配置走，重装插件也不丢。
所有键的默认值集中在 DEFAULTS，「插件设置…」对话框据此读写。
"""

from qgis.core import QgsSettings

PREFIX = 'point_bearing_tool'

# ---------- 默认值（单一事实来源） ----------
DEFAULTS = {
    # 视域计算
    'observer_height': 30.0,        # 观察者高于地面（米），默认模拟 30m 基站铁塔
    'target_height': 0.0,           # 目标自身高度（米）
    'tolerance': 0.02,              # 仰角容差（正切值），越大越容易判"可见"
    'curvature': True,              # 地球曲率修正
    'quality': 'standard',          # 计算质量 fast / standard / fine
    # 显示样式：视域
    'vs_fill_color': '#3cdc64',     # 可见区填充色
    'vs_fill_alpha': 80,            # 可见区不透明度 0-255
    'vs_ring_color': '#ffffff',     # 视距辅助圆
    'vs_ring_width': 1,
    'vs_ring_alpha': 255,
    # 显示样式：两点测距
    'measure_color': '#ff8c00',
    'measure_width': 2,
    'measure_alpha': 255,
    # 显示样式：范围圈
    'radius_color': '#e31b1b',
    'radius_width': 3,
    'radius_alpha': 255,
    'radius_label_pos': 'spoke',      # spoke 半径线中点 / border 圆周顶部
}

QUALITY_FACTORS = {'fast': 0.5, 'standard': 1.0, 'fine': 2.0}
QUALITY_LABELS = (
    ('fast', '快速（约快 2 倍，边缘略粗糙）'),
    ('standard', '标准'),
    ('fine', '精细（约慢 4 倍，边缘平滑）'),
)


class Settings(object):
    """读写插件设置（每个属性即时读 QgsSettings，改了立刻生效）"""

    def __init__(self):
        self.s = QgsSettings()

    def _key(self, name):
        return '%s/%s' % (PREFIX, name)

    def get(self, name, default=None):
        return self.s.value(self._key(name), default)

    def set(self, name, value):
        self.s.setValue(self._key(name), value)

    # ---------- 带类型/默认值读取 ----------
    def _typed(self, name, cast):
        default = DEFAULTS[name]
        raw = self.s.value(self._key(name), default)
        try:
            if cast is bool:
                if isinstance(raw, str):
                    return raw.strip().lower() in ('1', 'true', 'yes', 'on')
                return bool(raw)
            if cast is float:
                return float(raw)
            if cast is int:
                return int(raw)
            value = str(raw)
            if name.endswith('_color') and not value.startswith('#'):
                return default
            return value
        except (TypeError, ValueError):
            return default

    def _color(self, color_key, alpha_key=None):
        from qgis.PyQt.QtGui import QColor
        color = QColor(self._typed(color_key, str))
        if not color.isValid():
            color = QColor(DEFAULTS[color_key])
        if alpha_key is not None:
            color.setAlpha(self._typed(alpha_key, int))
        return color

    # ---------- DEM ----------
    @property
    def dem_layer_id(self):
        return self.get('dem_layer_id', '')

    @dem_layer_id.setter
    def dem_layer_id(self, value):
        self.set('dem_layer_id', value or '')

    # ---------- 视域计算 ----------
    @property
    def observer_height(self):
        """观察者高于地面的高度（米）

        默认 30m：模拟基站铁塔。观察点高程 = DEM 地面高程 + 该值。
        """
        return self._typed('observer_height', float)

    @observer_height.setter
    def observer_height(self, value):
        self.set('observer_height', float(value))

    @property
    def target_height(self):
        return self._typed('target_height', float)

    @target_height.setter
    def target_height(self, value):
        self.set('target_height', float(value))

    @property
    def tolerance(self):
        return self._typed('tolerance', float)

    @tolerance.setter
    def tolerance(self, value):
        self.set('tolerance', float(value))

    @property
    def curvature(self):
        return self._typed('curvature', bool)

    @curvature.setter
    def curvature(self, value):
        self.set('curvature', bool(value))

    @property
    def quality(self):
        value = self._typed('quality', str)
        return value if value in QUALITY_FACTORS else 'standard'

    @quality.setter
    def quality(self, value):
        self.set('quality', value if value in QUALITY_FACTORS else 'standard')

    def quality_factor(self):
        return QUALITY_FACTORS[self.quality]

    # ---------- 显示样式：视域 ----------
    @property
    def vs_fill_color(self):
        return self._typed('vs_fill_color', str)

    @vs_fill_color.setter
    def vs_fill_color(self, value):
        self.set('vs_fill_color', value)

    @property
    def vs_fill_alpha(self):
        return self._typed('vs_fill_alpha', int)

    @vs_fill_alpha.setter
    def vs_fill_alpha(self, value):
        self.set('vs_fill_alpha', int(value))

    @property
    def vs_ring_color(self):
        return self._typed('vs_ring_color', str)

    @vs_ring_color.setter
    def vs_ring_color(self, value):
        self.set('vs_ring_color', value)

    @property
    def vs_ring_width(self):
        return self._typed('vs_ring_width', int)

    @vs_ring_width.setter
    def vs_ring_width(self, value):
        self.set('vs_ring_width', int(value))

    @property
    def vs_ring_alpha(self):
        return self._typed('vs_ring_alpha', int)

    @vs_ring_alpha.setter
    def vs_ring_alpha(self, value):
        self.set('vs_ring_alpha', int(value))

    def viewshed_fill_qcolor(self):
        return self._color('vs_fill_color', 'vs_fill_alpha')

    def viewshed_ring_qcolor(self):
        return self._color('vs_ring_color', 'vs_ring_alpha')

    # ---------- 显示样式：两点测距 ----------
    @property
    def measure_color(self):
        return self._typed('measure_color', str)

    @measure_color.setter
    def measure_color(self, value):
        self.set('measure_color', value)

    @property
    def measure_width(self):
        return self._typed('measure_width', int)

    @measure_width.setter
    def measure_width(self, value):
        self.set('measure_width', int(value))

    @property
    def measure_alpha(self):
        return self._typed('measure_alpha', int)

    @measure_alpha.setter
    def measure_alpha(self, value):
        self.set('measure_alpha', int(value))

    def measure_qcolor(self):
        return self._color('measure_color', 'measure_alpha')

    # ---------- 显示样式：范围圈 ----------
    @property
    def radius_color(self):
        return self._typed('radius_color', str)

    @radius_color.setter
    def radius_color(self, value):
        self.set('radius_color', value)

    @property
    def radius_width(self):
        return self._typed('radius_width', int)

    @radius_width.setter
    def radius_width(self, value):
        self.set('radius_width', int(value))

    @property
    def radius_alpha(self):
        return self._typed('radius_alpha', int)

    @radius_alpha.setter
    def radius_alpha(self, value):
        self.set('radius_alpha', int(value))

    def radius_qcolor(self):
        return self._color('radius_color', 'radius_alpha')

    @property
    def radius_label_pos(self):
        """半径标签位置：'spoke' 半径线中点 / 'border' 圆周顶部"""
        value = self._typed('radius_label_pos', str)
        return value if value in ('spoke', 'border') else 'spoke'

    @radius_label_pos.setter
    def radius_label_pos(self, value):
        self.set('radius_label_pos',
                 value if value in ('spoke', 'border') else 'spoke')

    # ---------- 恢复默认 ----------
    def reset_to_defaults(self):
        for name, value in DEFAULTS.items():
            self.set(name, value)
