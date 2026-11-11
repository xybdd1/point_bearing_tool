# -*- coding: utf-8 -*-
"""
plugin.py - 插件主类

不提供停靠面板：点按钮即进入"两点测量"地图工具，
测完的结果直接画在地图上（线条终点处标注距离与方位角）。
"""

import os

from qgis.PyQt.QtCore import QCoreApplication, QSettings, QTranslator, QTimer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .map_tool import TwoPointMeasureTool

PLUGIN_NAME = '两点测距与方位角'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class PointBearingPlugin(object):

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = PLUGIN_DIR
        self.action = None
        self.clear_action = None
        self.tool = None
        self.translator = None
        self._install_translator()

    def _install_translator(self):
        locale = QSettings().value('locale/userLocale', '')
        if not isinstance(locale, str) or len(locale) < 2:
            return
        qm = os.path.join(self.plugin_dir, 'i18n', f'point_bearing_tool_{locale[0:2]}.qm')
        if os.path.exists(qm):
            self.translator = QTranslator()
            if self.translator.load(qm):
                QCoreApplication.installTranslator(self.translator)

    def _icon(self):
        for name in ('icon.svg', 'icon.png'):
            path = os.path.join(self.plugin_dir, name)
            if os.path.exists(path):
                return QIcon(path)
        return QIcon()

    # ---------- 生命周期 ----------
    def initGui(self):
        self.action = QAction(self._icon(), PLUGIN_NAME, self.iface.mainWindow())
        self.action.setObjectName('pointBearingToolAction')
        self.action.setToolTip('在地图上点取两点，距离与方位角直接标在图上')
        self.action.triggered.connect(self.run)

        self.clear_action = QAction('清除测量标注', self.iface.mainWindow())
        self.clear_action.setObjectName('pointBearingClearAction')
        self.clear_action.setToolTip('清除地图上的测量线与结果标注')
        self.clear_action.triggered.connect(self.clear)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.clear_action)

        # 地图工具切换时同步按钮状态
        try:
            self.iface.mapCanvas().mapToolSet.connect(self._on_map_tool_changed)
        except (AttributeError, TypeError):
            pass

    def unload(self):
        if self.tool is not None:
            try:
                if self.iface.mapCanvas().mapTool() is self.tool:
                    self.iface.mapCanvas().unsetMapTool(self.tool)
            except (AttributeError, RuntimeError):
                pass
            self.tool = None

        try:
            self.iface.mapCanvas().mapToolSet.disconnect(self._on_map_tool_changed)
        except (AttributeError, TypeError, RuntimeError):
            pass

        if self.action is not None:
            self.iface.removePluginMenu(PLUGIN_NAME, self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None

        if self.clear_action is not None:
            self.iface.removePluginMenu(PLUGIN_NAME, self.clear_action)
            self.clear_action = None

    # ---------- 行为 ----------
    def run(self):
        canvas = self.iface.mapCanvas()

        if self.tool is None:
            self.tool = TwoPointMeasureTool(canvas, self.iface)
            self.tool.measurementDone.connect(self._on_measurement_done)

        # 重新激活：保留已有标注，但允许接着点新的
        self.tool.reset_measurement()
        canvas.setMapTool(self.tool)

        self._push_hint('左键点取 A、B 两点；右键或 Esc 结束')

    def clear(self):
        if self.tool is not None:
            self.tool.reset_measurement()
        self._push_hint('已清除测量标注')

    def _on_measurement_done(self, measurement):
        pass

    def _on_map_tool_changed(self, new_tool):
        if self.action is None:
            return
        is_self = (new_tool is self.tool)
        self.action.setCheckable(is_self)
        if is_self:
            self.action.setChecked(True)

    def _push_hint(self, text, duration=4):
        try:
            self.iface.messageBar().pushMessage('两点测距', text, level=0, duration=duration)
        except (AttributeError, TypeError):
            pass
