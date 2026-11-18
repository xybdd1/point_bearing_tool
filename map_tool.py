# -*- coding: utf-8 -*-
"""
map_tool.py - 地图上两点测距与方位角

交互：
    左键点两下 → 取 A、B 两点，在线条终点 B 处直接显示距离与方位角
    移动鼠标时（已取 A 点）→ 实时预览，标签跟着光标
    右键 / Esc → 结束，保留已画出的结果
    再次左键 → 开始新的一次测量
"""

from qgis.core import QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from . import geodesy
from .label_item import MeasureLabelItem
from .settings import Settings


class TwoPointMeasureTool(QgsMapToolEmitPoint):
    """两点测距 / 方位角地图工具（结果直接标注在地图上）"""

    measurementDone = pyqtSignal(object)     # geodesy.Measurement
    cleared = pyqtSignal()

    def __init__(self, canvas, iface=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self.settings = Settings()

        self.point_a = None
        self.point_b = None
        self._previewing = False

        # A-B 连线
        self.line_rb = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.LineGeometry)

        # A、B 点标记
        self.marker_a = self._make_marker('#27ae60')
        self.marker_b = self._make_marker('#c0392b')

        # 结果标签
        self.label = MeasureLabelItem(self.canvas)

        # 画布每次重绘后同步标签位置（缩放/平移时贴住锚点）
        try:
            self.canvas.mapCanvasRefresh.connect(self.label.updatePosition)
        except (AttributeError, TypeError):
            self.canvas.renderComplete.connect(self.label.updatePosition)

        self._toast = _Toast(iface)

    def _apply_styles(self):
        """从设置读测距线颜色/粗细/透明度"""
        try:
            self.line_rb.setColor(self.settings.measure_qcolor())
            self.line_rb.setWidth(max(1, self.settings.measure_width))
        except (AttributeError, TypeError, RuntimeError):
            pass

    def _make_marker(self, color):
        rb = QgsRubberBand(self.canvas, QgsWkbTypes.GeometryType.PointGeometry)
        rb.setColor(QColor(color))
        rb.setIconSize(11)
        rb.setWidth(2)
        return rb

    # ---------- 生命周期 ----------
    def activate(self):
        super().activate()
        self._apply_styles()
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        self._toast.hint('左键点两点测量，右键结束')

    def deactivate(self):
        self._toast.clear()
        super().deactivate()

    def reset_measurement(self):
        """清除当前测量，准备下一轮"""
        self.point_a = None
        self.point_b = None
        self._previewing = False
        for rb in (self.line_rb, self.marker_a, self.marker_b):
            rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
        self.label.clear()
        self.canvas.refresh()
        self.cleared.emit()

    def clear_all(self):
        self.reset_measurement()

    # ---------- 坐标换算 ----------
    def _to_wgs84(self, point):
        crs = self.canvas.mapSettings().destinationCrs()
        return geodesy.to_wgs84(point, crs)

    def _label_lines(self, measurement, unit_label_fn=None):
        """组装标签文字"""
        if unit_label_fn is not None:
            dist_text = unit_label_fn(measurement.distance_m)
        else:
            dist_text = geodesy.format_distance(measurement.distance_m)

        if measurement.bearing_deg is None:
            bearing_text = '方位角 —'
        else:
            bearing_text = (f'方位角 {measurement.bearing_deg:.3f}°'
                            f' ({measurement.compass})')
        return [f'距离 {dist_text}', bearing_text]

    # ---------- 事件 ----------
    def canvasMoveEvent(self, event):
        if self.point_a is None or self.point_b is not None:
            return

        current = self.toMapCoordinates(event.pos())
        self._previewing = True

        # 连线预览
        self.line_rb.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self.line_rb.addPoint(self.point_a)
        self.line_rb.addPoint(current)
        self.line_rb.show()

        # 标签预览
        try:
            a = self._to_wgs84(self.point_a)
            b = self._to_wgs84(current)
            m = geodesy.Measurement(a, b)
            self.label.set_label(current, self._label_lines(m))
        except Exception:
            pass

        self.canvas.refresh()

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        point = self.toMapCoordinates(event.pos())

        # 已有完整测量 → 左键开始新的一次
        if self.point_a is not None and self.point_b is not None:
            self.reset_measurement()

        if self.point_a is None:
            self.point_a = point
            self.marker_a.reset(QgsWkbTypes.GeometryType.PointGeometry)
            self.marker_a.addPoint(point)
            self.marker_a.show()
            self._toast.hint('已取 A 点，再点 B 点')
            self.canvas.refresh()
            return

        self.point_b = point
        self._previewing = False
        self.marker_b.reset(QgsWkbTypes.GeometryType.PointGeometry)
        self.marker_b.addPoint(point)
        self.marker_b.show()

        self.line_rb.reset(QgsWkbTypes.GeometryType.LineGeometry)
        self.line_rb.addPoint(self.point_a)
        self.line_rb.addPoint(point)
        self.line_rb.show()

        try:
            a = self._to_wgs84(self.point_a)
            b = self._to_wgs84(point)
            m = geodesy.Measurement(a, b)
            self.label.set_label(point, self._label_lines(m))
            self._toast.hint(f'{m.distance_text} / '
                             f'{m.bearing_text}（{m.compass}）')
            self.measurementDone.emit(m)
        except Exception as e:
            print(f'[两点测距] 计算失败: {e}')
            self._toast.hint('计算失败，请重试')

        self.canvas.refresh()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._finish()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.reset_measurement()
            return
        super().keyPressEvent(event)

    def _finish(self):
        """右键/Esc：结束本次取点，但保留画好的结果"""
        if self.point_a is not None and self.point_b is None:
            # 只取了一个点，丢弃并清理
            self.reset_measurement()
        self._toast.clear()
        self.canvas.unsetMapTool(self)


class _Toast(object):
    """在地图消息栏短暂显示操作提示（没有 iface 时静默）"""

    def __init__(self, iface):
        self.iface = iface

    def hint(self, text, duration=3):
        if self.iface is None:
            return
        try:
            self.iface.messageBar().pushMessage('两点测距', text, level=0, duration=duration)
        except Exception:
            pass

    def clear(self):
        pass
