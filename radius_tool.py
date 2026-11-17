# -*- coding: utf-8 -*-
"""
radius_tool.py - 范围圈（等距环）工具

交互：
    左键点一下 → 确定圆心
    移动鼠标 → 实时显示以圆心为中心、光标到圆心距离为半径的圆
    左键再点 → 固定这个圆，结束
    右键 / Esc → 结束，保留已画的圆
    右键（无圆时）/ Delete → 清除
"""

from qgis.core import QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from . import geodesy
from .label_item import MeasureLabelItem


class RadiusCircleTool(QgsMapToolEmitPoint):
    """以一点为圆心、拖动距离为半径画等距圆"""

    radiusChanged = pyqtSignal(float)     # 当前半径（米）
    circleDrawn = pyqtSignal(object)      # (中心 QgsPointXY, 半径 float)

    def __init__(self, canvas, iface=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface

        self.center = None
        self.radius_m = 0.0
        self.locked = False

        # 圆（用折线闭合绘制，兼容各坐标系）
        self.circle_rb = QgsRubberBand(self.canvas,
                                       QgsWkbTypes.GeometryType.LineGeometry)
        self.circle_rb.setColor(QColor('#2980b9'))
        self.circle_rb.setWidth(2)

        # 半径指示线（圆心 → 光标）
        self.spoke_rb = QgsRubberBand(self.canvas,
                                      QgsWkbTypes.GeometryType.LineGeometry)
        self.spoke_rb.setColor(QColor('#2980b9'))
        self.spoke_rb.setWidth(1)
        self.spoke_rb.setLineStyle(Qt.PenStyle.DashLine)

        # 圆心标记
        self.center_marker = QgsRubberBand(self.canvas,
                                           QgsWkbTypes.GeometryType.PointGeometry)
        self.center_marker.setColor(QColor('#c0392b'))
        self.center_marker.setIconSize(11)
        self.center_marker.setWidth(2)

        # 半径标签
        self.label = MeasureLabelItem(self.canvas)

        try:
            self.canvas.mapCanvasRefresh.connect(self.label.updatePosition)
        except (AttributeError, TypeError):
            self.canvas.renderComplete.connect(self.label.updatePosition)

        self._toast = _Toast(iface)

        # 画布控件尺寸变化时重画圆（避免圆变形）
        try:
            self.canvas.canvasColorChanged.connect(lambda *_: None)
        except (AttributeError, TypeError):
            pass

    # ---------- 生命周期 ----------
    def activate(self):
        super().activate()
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        self._toast.hint('左键确定圆心，移动鼠标调整半径')

    def deactivate(self):
        self._toast.clear()
        super().deactivate()

    def reset_all(self):
        """清除圆与圆心"""
        self.center = None
        self.radius_m = 0.0
        self.locked = False
        for rb in (self.circle_rb, self.spoke_rb, self.center_marker):
            rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
        self.label.clear()
        self.canvas.refresh()

    # ---------- 绘制 ----------
    def _to_wgs84(self, point):
        crs = self.canvas.mapSettings().destinationCrs()
        return geodesy.to_wgs84(point, crs)

    def _from_wgs84(self, point):
        crs = self.canvas.mapSettings().destinationCrs()
        return geodesy.from_wgs84(point, crs)

    def _draw_circle(self, cursor_point):
        """按当前圆心和光标位置重画圆"""
        if self.center is None:
            return

        center_wgs = self._to_wgs84(self.center)
        cursor_wgs = self._to_wgs84(cursor_point)
        self.radius_m = geodesy.distance_meters(center_wgs, cursor_wgs)

        if self.radius_m <= 0:
            return

        # 严格的地面等距圆：每个方向到圆心的真实距离都等于半径
        ring_wgs = geodesy.circle_points(center_wgs, self.radius_m, segments=180)
        ring_canvas = [self._from_wgs84(p) for p in ring_wgs]

        self.circle_rb.reset(QgsWkbTypes.GeometryType.LineGeometry)
        for p in ring_canvas:
            self.circle_rb.addPoint(p, True)
        # 闭合
        self.circle_rb.addPoint(ring_canvas[0], True)
        self.circle_rb.show()

        # 半径指示线
        self.spoke_rb.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self.spoke_rb.addPoint(self.center)
        self.spoke_rb.addPoint(cursor_point)
        self.spoke_rb.show()

        # 标签放在圆周上方（正北方向），只显示半径
        north_wgs = geodesy.destination_point(center_wgs, 0, self.radius_m)
        label_point = self._from_wgs84(north_wgs)

        self.label.set_label(
            label_point,
            [f'半径 {geodesy.format_distance(self.radius_m)}'])

        self.radiusChanged.emit(self.radius_m)
        self.canvas.refresh()

    # ---------- 事件 ----------
    def canvasMoveEvent(self, event):
        if self.center is None or self.locked:
            return
        self._draw_circle(self.toMapCoordinates(event.pos()))
        if self.radius_m > 0:
            self._toast.hint(
                f'半径 {geodesy.format_distance(self.radius_m)}（左键固定）')

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        point = self.toMapCoordinates(event.pos())

        # 还没定圆心 → 定圆心
        if self.center is None:
            self.center = point
            self.center_marker.reset(QgsWkbTypes.GeometryType.PointGeometry)
            self.center_marker.addPoint(point)
            self.center_marker.show()
            self.locked = False
            self.radius_m = 0.0
            # 圆心刚定、鼠标还没移动，先不画圆，避免出现一个突兀的大圈
            self.circle_rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
            self.spoke_rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
            self.label.clear()
            self._toast.hint('圆心已定，移动鼠标调整半径')
            self.canvas.refresh()
            return

        # 已有圆 → 先清理再重新定圆心
        if self.locked:
            self.reset_all()
            self.center = point
            self.center_marker.reset(QgsWkbTypes.GeometryType.PointGeometry)
            self.center_marker.addPoint(point)
            self.center_marker.show()
            self.locked = False
            self._toast.hint('圆心已重新确定，移动鼠标调整半径')
            self.canvas.refresh()
            return

        # 固定当前圆
        self._draw_circle(point)
        self.locked = True
        self._toast.hint(
            f'半径 {geodesy.format_distance(self.radius_m)}，已固定。'
            f'左键重新画，右键结束')
        self.circleDrawn.emit((QgsPointXY(self.center), self.radius_m))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._finish()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.reset_all()
            return
        super().keyPressEvent(event)

    def _finish(self):
        """右键/Esc：保留已画的圆并退出工具"""
        self._toast.clear()
        self.canvas.unsetMapTool(self)


class _Toast(object):
    def __init__(self, iface):
        self.iface = iface

    def hint(self, text, duration=3):
        if self.iface is None:
            return
        try:
            self.iface.messageBar().pushMessage('范围圈', text, level=0, duration=duration)
        except (AttributeError, TypeError):
            pass

    def clear(self):
        pass
