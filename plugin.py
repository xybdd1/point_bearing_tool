# -*- coding: utf-8 -*-
"""
plugin.py - 插件主类

三个地图工具，都是点按钮进工具、右键退出：
    1. 两点测距与方位角 —— 点两下，距离/方位角标在线条终点
    2. 范围圈           —— 点圆心，拖动半径，画出等距圆
    3. 视域分析         —— 点观察点 + 拖视距，用 DEM 算可见范围
"""

import os

from qgis.core import QgsProject, QgsRasterLayer
from qgis.PyQt.QtCore import QCoreApplication, QSettings, QTranslator, QTimer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .map_tool import TwoPointMeasureTool
from .radius_tool import RadiusCircleTool
from .settings import Settings
from .viewshed_tool import ViewshedTool

PLUGIN_NAME = '两点测距与方位角'
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


class PointBearingPlugin(object):

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = PLUGIN_DIR
        self.settings = Settings()
        self.action = None
        self.radius_action = None
        self.viewshed_action = None
        self.dem_action = None
        self.clear_action = None
        self.tool = None
        self.radius_tool = None
        self.viewshed_tool = None
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
        return self._load_icon('icon.svg')

    def _radius_icon(self):
        return self._load_icon('icon_radius.svg')

    def _viewshed_icon(self):
        return self._load_icon('icon_viewshed.svg')

    def _load_icon(self, filename):
        for name in (filename, 'icon.svg', 'icon.png'):
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

        self.radius_action = QAction(self._radius_icon(), '范围圈（半径）',
                                     self.iface.mainWindow())
        self.radius_action.setObjectName('pointBearingRadiusAction')
        self.radius_action.setToolTip('点一个圆心，拖动鼠标画出等距范围圈')
        self.radius_action.triggered.connect(self.run_radius)

        self.viewshed_action = QAction(self._viewshed_icon(), '视域分析（可见范围）',
                                       self.iface.mainWindow())
        self.viewshed_action.setObjectName('pointBearingViewshedAction')
        self.viewshed_action.setToolTip('点观察点后拖出视距，用 DEM 算该点能看到的范围')
        self.viewshed_action.triggered.connect(self.run_viewshed)

        self.dem_action = QAction('设置高程(DEM)图层…', self.iface.mainWindow())
        self.dem_action.setObjectName('pointBearingDemAction')
        self.dem_action.setToolTip('选择用于视域分析的高程栅格，只需设置一次')
        self.dem_action.triggered.connect(lambda: self.choose_dem(show_message=True))

        self.clear_action = QAction('清除全部标注', self.iface.mainWindow())
        self.clear_action.setObjectName('pointBearingClearAction')
        self.clear_action.setToolTip('清除地图上的测距线、范围圈、视域结果和标注')
        self.clear_action.triggered.connect(self.clear)

        self.iface.addToolBarIcon(self.action)
        self.iface.addToolBarIcon(self.radius_action)
        self.iface.addToolBarIcon(self.viewshed_action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.radius_action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.viewshed_action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.dem_action)
        self.iface.addPluginToMenu(PLUGIN_NAME, self.clear_action)

        # 地图工具切换时同步按钮状态
        try:
            self.iface.mapCanvas().mapToolSet.connect(self._on_map_tool_changed)
        except (AttributeError, TypeError):
            pass

    def unload(self):
        canvas = self.iface.mapCanvas()
        for tool in (self.tool, self.radius_tool, self.viewshed_tool):
            if tool is not None:
                try:
                    if canvas.mapTool() is tool:
                        canvas.unsetMapTool(tool)
                except (AttributeError, RuntimeError):
                    pass
        self.tool = None
        self.radius_tool = None
        self.viewshed_tool = None

        try:
            canvas.mapToolSet.disconnect(self._on_map_tool_changed)
        except (AttributeError, TypeError, RuntimeError):
            pass

        for act in (self.action, self.radius_action, self.viewshed_action,
                    self.dem_action, self.clear_action):
            if act is not None:
                try:
                    self.iface.removePluginMenu(PLUGIN_NAME, act)
                    self.iface.removeToolBarIcon(act)
                except (AttributeError, TypeError):
                    pass
        self.action = None
        self.radius_action = None
        self.viewshed_action = None
        self.dem_action = None
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

        self._push_hint('左键点取 A、B 两点；右键或 Esc 结束', '两点测距')

    def run_radius(self):
        canvas = self.iface.mapCanvas()

        if self.radius_tool is None:
            self.radius_tool = RadiusCircleTool(canvas, self.iface)

        # 每次进来都从干净状态开始
        self.radius_tool.reset_all()
        canvas.setMapTool(self.radius_tool)

        self._push_hint('左键点圆心，移动鼠标调整半径；右键或 Esc 结束', '范围圈')

    def run_viewshed(self):
        canvas = self.iface.mapCanvas()

        # 首次使用先确认 DEM
        if self._current_dem() is None:
            dem = self.choose_dem(show_message=True)
            if dem is None:
                self._push_hint('视域分析需要先选择高程(DEM)图层', '视域分析')
                return

        if self.viewshed_tool is None:
            self.viewshed_tool = ViewshedTool(canvas, self.iface, self)
            self.viewshed_tool.analysisFailed.connect(
                lambda msg: self._push_hint(msg, '视域分析'))

        self.viewshed_tool.reset_all()
        canvas.setMapTool(self.viewshed_tool)

        self._push_hint('左键点观察点，移动鼠标设视距，再左键计算视域', '视域分析')

    # ---------- DEM 选择 ----------
    def _current_dem(self):
        """返回当前设置里有效的 DEM 图层，没有则 None"""
        lid = self.settings.dem_layer_id
        if not lid:
            return None
        layer = QgsProject.instance().mapLayer(lid)
        if isinstance(layer, QgsRasterLayer) and layer.isValid():
            return layer
        return None

    def choose_dem(self, show_message=True):
        """让用户选一个 DEM 栅格图层，并记住选择

        优先用项目里已有的栅格图层；没有的话弹文件选择框。
        """
        from qgis.PyQt.QtWidgets import QInputDialog, QFileDialog, QMessageBox

        rasters = [lyr for lyr in QgsProject.instance().mapLayers().values()
                   if isinstance(lyr, QgsRasterLayer) and lyr.isValid()]

        if rasters:
            names = [f'{l.name()}  [{l.crs().authid()}]' for l in rasters]
            item, ok = QInputDialog.getItem(
                self.iface.mainWindow(), '选择高程(DEM)图层',
                '用于视域分析的高程栅格：', names, 0, False)
            if not ok:
                return None
            idx = names.index(item) if item in names else 0
            layer = rasters[idx]
        else:
            path, _f = QFileDialog.getOpenFileName(
                self.iface.mainWindow(), '选择高程(DEM)文件', '',
                '栅格文件 (*.tif *.tiff *.img *.asc *.hgt *.vrt);;所有文件 (*)')
            if not path:
                return None
            layer = QgsRasterLayer(path, os.path.basename(path))
            if not layer.isValid():
                if show_message:
                    QMessageBox.warning(self.iface.mainWindow(), '视域分析',
                                        '这个文件打不开，可能不是有效的栅格。')
                return None
            QgsProject.instance().addMapLayer(layer)

        self.settings.dem_layer_id = layer.id()

        if show_message:
            dem_crs = layer.crs()
            note = ''
            if dem_crs.isGeographic():
                note = '（经纬度坐标系，内部会按该纬度换算米）'
            self._push_hint(
                '已选用 DEM：%s  %dx%d%s' % (layer.name(), layer.width(),
                                            layer.height(), note),
                '视域分析', 6)
        return layer

    def clear(self):
        if self.tool is not None:
            self.tool.reset_measurement()
        if self.radius_tool is not None:
            self.radius_tool.reset_all()
        if self.viewshed_tool is not None:
            self.viewshed_tool.reset_all()
        self._push_hint('已清除全部标注', '两点测距')

    def _on_measurement_done(self, measurement):
        pass

    def _on_map_tool_changed(self, new_tool):
        pairs = ((self.action, self.tool),
                 (self.radius_action, self.radius_tool),
                 (self.viewshed_action, self.viewshed_tool))
        for action, tool in pairs:
            if action is None:
                continue
            is_self = (new_tool is tool)
            action.setCheckable(is_self)
            if is_self:
                action.setChecked(True)

    def _push_hint(self, text, title='两点测距', duration=4):
        try:
            self.iface.messageBar().pushMessage(title, text, level=0, duration=duration)
        except (AttributeError, TypeError):
            pass
