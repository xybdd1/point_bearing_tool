# -*- coding: utf-8 -*-
"""
settings_dialog.py - 「插件设置…」对话框

两个标签页：
    视域计算：观察者高度 / 目标高度 / 仰角容差 / 曲率修正 / 计算质量
    显示样式：视域填充、视距圆、测距线、范围圈 的颜色 / 粗细 / 透明度

所有值即时读写 Settings（QgsSettings），「恢复默认」一次写回 DEFAULTS。
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .settings import DEFAULTS, QUALITY_LABELS, Settings


class _ColorButton(QPushButton):
    """显示当前颜色、点击弹 QColorDialog 的小按钮"""

    def __init__(self, hex_color, parent=None):
        super().__init__(parent)
        self._color = QColor(hex_color)
        if not self._color.isValid():
            self._color = QColor(DEFAULTS.get('vs_fill_color', '#3cdc64'))
        self.setFixedWidth(72)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self._pick)
        self._refresh()

    def color(self):
        return QColor(self._color)

    def set_color(self, hex_color):
        color = QColor(hex_color)
        if color.isValid():
            self._color = color
            self._refresh()

    def _pick(self):
        picked = QColorDialog.getColor(self._color, self, '选择颜色')
        if picked.isValid():
            picked.setAlpha(255)   # 透明度由滑条单独控制
            self._color = picked
            self._refresh()

    def _refresh(self):
        self.setText(self._color.name().upper())
        self.setStyleSheet(
            'background-color: %s; color: %s; border: 1px solid #888;'
            ' padding: 2px 6px;' % (
                self._color.name(),
                '#fff' if self._color.lightness() < 128 else '#333'))


class _AlphaRow(QWidget):
    """不透明度滑条 + 数值标签（0-255）"""

    def __init__(self, value, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 255)
        self.slider.setValue(int(value))
        self.value_label = QLabel()
        self.slider.valueChanged.connect(self._update)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.value_label)
        self._update(self.slider.value())

    def value(self):
        return self.slider.value()

    def set_value(self, value):
        self.slider.setValue(int(value))

    def _update(self, value):
        self.value_label.setText('%d%%（%d）' % (round(value * 100 / 255), value))


class SettingsDialog(QDialog):
    """插件设置对话框"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = Settings()
        self.setWindowTitle('测量工具箱 - 插件设置')
        self.setMinimumWidth(460)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_viewshed_tab(), '视域计算')
        self.tabs.addTab(self._build_style_tab(), '显示样式')

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        restore = buttons.addButton('恢复默认',
                                     QDialogButtonBox.ButtonRole.ResetRole)
        buttons.accepted.connect(self._accept_settings)
        buttons.rejected.connect(self.reject)
        restore.clicked.connect(self._restore_defaults)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)

        self._load_from_settings()

    # ---------- 构建 ----------
    def _build_viewshed_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        group = QGroupBox('观察点')
        form = QFormLayout(group)
        self.spin_observer = QDoubleSpinBox()
        self.spin_observer.setRange(0.0, 500.0)
        self.spin_observer.setSingleStep(1.0)
        self.spin_observer.setDecimals(1)
        self.spin_observer.setSuffix(' 米')
        form.addRow('观察者高度（塔高）：', self.spin_observer)
        self.spin_target = QDoubleSpinBox()
        self.spin_target.setRange(0.0, 100.0)
        self.spin_target.setSingleStep(0.5)
        self.spin_target.setDecimals(1)
        self.spin_target.setSuffix(' 米')
        form.addRow('目标高度：', self.spin_target)
        layout.addWidget(group)

        group = QGroupBox('算法')
        form = QFormLayout(group)
        self.spin_tolerance = QDoubleSpinBox()
        self.spin_tolerance.setRange(0.0, 0.2)
        self.spin_tolerance.setSingleStep(0.005)
        self.spin_tolerance.setDecimals(3)
        form.addRow('仰角容差（越大越易判可见）：', self.spin_tolerance)
        self.check_curvature = QCheckBox('地球曲率修正')
        form.addRow(self.check_curvature)
        self.combo_quality = QComboBox()
        for code, label in QUALITY_LABELS:
            self.combo_quality.addItem(label, code)
        form.addRow('计算质量：', self.combo_quality)
        layout.addWidget(group)

        hint = QLabel('提示：塔高 30m 时观察点位于 30m 高处，能越过更多近处山脊；'
                      '容差设 0 最严格，可见范围会明显变小。')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #666;')
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _build_style_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        self._style_rows = {}

        def add_group(title, color_key, width_key, alpha_key,
                      with_width=True):
            group = QGroupBox(title)
            form = QFormLayout(group)
            color_btn = _ColorButton(self.settings._typed(color_key, str))
            form.addRow('颜色：', color_btn)
            width_spin = None
            if width_key:
                width_spin = QSpinBox()
                width_spin.setRange(0, 10)
                width_spin.setSuffix(' px')
                form.addRow('线宽：', width_spin)
            alpha_row = _AlphaRow(self.settings._typed(alpha_key, int))
            form.addRow('不透明度：', alpha_row)
            layout.addWidget(group)
            self._style_rows[title] = (color_key, width_key, alpha_key,
                                       color_btn, width_spin, alpha_row)
            return color_btn, width_spin, alpha_row

        add_group('视域 - 可见区域填充', 'vs_fill_color', None, 'vs_fill_alpha')
        add_group('视域 - 视距辅助圆', 'vs_ring_color', 'vs_ring_width',
                  'vs_ring_alpha')
        add_group('两点测距 - 连线', 'measure_color', 'measure_width',
                  'measure_alpha')
        add_group('范围圈 - 圆周', 'radius_color', 'radius_width',
                  'radius_alpha')

        layout.addStretch(1)
        return page

    # ---------- 读写 ----------
    def _load_from_settings(self):
        s = self.settings
        self.spin_observer.setValue(s.observer_height)
        self.spin_target.setValue(s.target_height)
        self.spin_tolerance.setValue(s.tolerance)
        self.check_curvature.setChecked(s.curvature)
        index = self.combo_quality.findData(s.quality)
        self.combo_quality.setCurrentIndex(max(0, index))

        for title, (color_key, width_key, alpha_key,
                    color_btn, width_spin, alpha_row) in self._style_rows.items():
            color_btn.set_color(s._typed(color_key, str))
            if width_spin is not None:
                width_spin.setValue(s._typed(width_key, int))
            alpha_row.set_value(s._typed(alpha_key, int))

    def _accept_settings(self):
        s = self.settings
        s.observer_height = self.spin_observer.value()
        s.target_height = self.spin_target.value()
        s.tolerance = self.spin_tolerance.value()
        s.curvature = self.check_curvature.isChecked()
        s.quality = self.combo_quality.currentData()

        for title, (color_key, width_key, alpha_key,
                    color_btn, width_spin, alpha_row) in self._style_rows.items():
            s.set(color_key, color_btn.color().name())
            if width_spin is not None:
                s.set(width_key, width_spin.value())
            s.set(alpha_key, alpha_row.value())

        s.dem_layer_id = s.dem_layer_id  # 保持原 DEM
        self.accept()

    def _restore_defaults(self):
        self.settings.reset_to_defaults()
        self._load_from_settings()
