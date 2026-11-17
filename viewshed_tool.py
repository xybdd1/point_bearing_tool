# -*- coding: utf-8 -*-
"""
viewshed_tool.py - 视域分析地图工具

交互（和范围圈工具一致）：
    左键点一下  -> 定观察点（圆心）
    移动鼠标    -> 半径跟着走，松手前实时显示范围
    左键再点    -> 按当前半径计算视域
    右键 / Esc  -> 结束，保留结果

视域计算依赖 DEM 高程图层；DEM 只需在首次使用时选一次，之后记住。
"""

import os

from qgis.core import (QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer,
                       QgsWkbTypes)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from . import geodesy
from . import viewshed as vs_mod
from .label_item import MeasureLabelItem
from .settings import Settings


class ViewshedTool(QgsMapToolEmitPoint):
    """视域分析：定观察点 + 拖半径"""

    analysisStarted = pyqtSignal()
    analysisDone = pyqtSignal(object)     # 计算结果 dict
    analysisFailed = pyqtSignal(str)

    def __init__(self, canvas, iface=None, plugin=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self.plugin = plugin
        self.settings = Settings()

        self.center = None
        self.radius_m = 0.0
        self.locked = False

        # 半径圈（起辅助作用，浅色虚线）
        self.radius_rb = QgsRubberBand(self.canvas,
                                       QgsWkbTypes.GeometryType.LineGeometry)
        self.radius_rb.setColor(QColor('#ffffff'))
        self.radius_rb.setWidth(1)
        self.radius_rb.setLineStyle(Qt.PenStyle.DashLine)

        # 观察点
        self.center_marker = QgsRubberBand(self.canvas,
                                           QgsWkbTypes.GeometryType.PointGeometry)
        self.center_marker.setColor(QColor('#ff8c00'))
        self.center_marker.setIconSize(13)
        self.center_marker.setWidth(3)

        # 结果图层（可见区域）
        self.result_rb = QgsRubberBand(self.canvas,
                                       QgsWkbTypes.GeometryType.PolygonGeometry)
        self.result_rb.setColor(QColor(60, 220, 100, 80))
        if hasattr(self.result_rb, 'setStrokeColor'):
            self.result_rb.setStrokeColor(QColor(60, 220, 100, 200))
        self.result_rb.setWidth(1)

        # 标签
        self.label = MeasureLabelItem(self.canvas)
        try:
            self.canvas.mapCanvasRefresh.connect(self.label.updatePosition)
        except (AttributeError, TypeError):
            self.canvas.renderComplete.connect(self.label.updatePosition)

        self._toast = _Toast(iface)

    # ---------- 生命周期 ----------
    def activate(self):
        super().activate()
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)
        self._toast.hint('左键点观察点，移动鼠标设半径，再左键计算视域')

    def deactivate(self):
        self._toast.clear()
        super().deactivate()

    def reset_all(self):
        self.center = None
        self.radius_m = 0.0
        self.locked = False
        for rb in (self.radius_rb, self.center_marker, self.result_rb):
            rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
        self.label.clear()
        self.canvas.refresh()

    # ---------- 辅助 ----------
    def _to_wgs84(self, p):
        return geodesy.to_wgs84(p, self.canvas.mapSettings().destinationCrs())

    def _from_wgs84(self, p):
        return geodesy.from_wgs84(p, self.canvas.mapSettings().destinationCrs())

    def _dem_layer(self):
        """取 DEM 图层；找不到就提示并返回 None"""
        lid = self.settings.dem_layer_id
        layer = QgsProject.instance().mapLayer(lid) if lid else None
        if isinstance(layer, QgsRasterLayer) and layer.isValid():
            return layer

        # 记住的丢了，让用户选一次
        if self.plugin is not None and hasattr(self.plugin, 'choose_dem'):
            layer = self.plugin.choose_dem(show_message=True)
            if isinstance(layer, QgsRasterLayer) and layer.isValid():
                return layer
        return None

    # ---------- 交互 ----------
    def canvasMoveEvent(self, event):
        if self.center is None or self.locked:
            return
        cur = self.toMapCoordinates(event.pos())
        cw = self._to_wgs84(self.center)
        ew = self._to_wgs84(cur)
        self.radius_m = geodesy.distance_meters(cw, ew)
        if self.radius_m <= 0:
            return

        ring = geodesy.circle_points(cw, self.radius_m, segments=120)
        self.radius_rb.reset(QgsWkbTypes.GeometryType.LineGeometry)
        for p in ring:
            self.radius_rb.addPoint(self._from_wgs84(p), True)
        self.radius_rb.addPoint(self._from_wgs84(ring[0]), True)
        self.radius_rb.show()

        north = geodesy.destination_point(cw, 0, self.radius_m)
        self.label.set_label(self._from_wgs84(north),
                             [f'视距 {geodesy.format_distance(self.radius_m)}'])

        self._toast.hint(f'视距 {geodesy.format_distance(self.radius_m)}（左键计算）')
        self.canvas.refresh()

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        pos = self.toMapCoordinates(event.pos())

        # 定观察点
        if self.center is None:
            self.center = pos
            self.center_marker.reset(QgsWkbTypes.GeometryType.PointGeometry)
            self.center_marker.addPoint(pos)
            self.center_marker.show()
            self.radius_m = 0.0
            self.locked = False
            self.label.clear()
            self.radius_rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
            self._toast.hint('观察点已定，移动鼠标设视距')
            self.canvas.refresh()
            return

        # 已在结果状态 -> 重新开始
        if self.locked:
            self.reset_all()
            self.canvasReleaseEvent(event)
            return

        # 计算
        self.run_analysis()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._finish()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.reset_all()
            return
        super().keyPressEvent(event)

    # ---------- 计算 ----------
    def run_analysis(self):
        if self.center is None or self.radius_m <= 0:
            self._toast.hint('请先移动鼠标确定视距')
            return

        dem = self._dem_layer()
        if dem is None:
            self.analysisFailed.emit('未选择 DEM 高程图层')
            return

        # 在线图层 / 坏图层提前拦截，避免读几万像元把 QGIS 卡死
        ok, reason = vs_mod.check_dem_layer(dem)
        if not ok:
            self._toast.hint(reason.replace('\n', ' '), 10)
            self.analysisFailed.emit(reason)
            self._show_error(reason)
            return

        cfg = self.settings
        self._toast.hint('正在计算视域…')
        self.analysisStarted.emit()

        try:
            # DEM 坐标系 vs 画布坐标系
            canvas_crs = self.canvas.mapSettings().destinationCrs()
            dem_crs = dem.crs()

            center_canvas = QgsPointXY(self.center)
            if canvas_crs != dem_crs:
                from qgis.core import QgsCoordinateTransform
                tr = QgsCoordinateTransform(canvas_crs, dem_crs,
                                            QgsProject.instance())
                center_dem = tr.transform(center_canvas)
            else:
                center_dem = center_canvas

            result = vs_mod.compute_viewshed(
                dem, center_dem, self.radius_m,
                observer_height=cfg.observer_height,
                target_height=cfg.target_height,
                tolerance=cfg.tolerance)

            if result is None:
                self.analysisFailed.emit('计算失败：观察点可能不在 DEM 覆盖范围内')
                self._toast.hint('计算失败：观察点可能不在 DEM 覆盖范围内', 8)
                return

            self._draw_result(result, dem_crs, canvas_crs)
            self.locked = True

            vis, blk = result['visible_count'], result['blocked_count']
            tot = vis + blk
            pct = vis / tot * 100 if tot else 0
            self._toast.hint(
                '可见 %.1f%%（%d/%d 像元）  地面 %.0fm  左键重算，右键结束'
                % (pct, vis, tot, result['ground_z']), 8)
            self.analysisDone.emit(result)

        except vs_mod.ViewshedError as e:
            self._toast.hint(str(e).replace('\n', ' '), 10)
            self.analysisFailed.emit(str(e))
            self._show_error(str(e))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.analysisFailed.emit(str(e))
            self._toast.hint('计算出错: %s' % e, 8)
            self._show_error('计算出错：%s' % e)

    def _show_error(self, message):
        """重要错误用弹窗显示，避免消息栏一闪而过"""
        try:
            from qgis.PyQt.QtWidgets import QMessageBox
            QMessageBox.warning(self.iface.mainWindow() if self.iface else None,
                                '视域分析', message)
        except (AttributeError, RuntimeError):
            pass

    def _draw_result(self, result, dem_crs, canvas_crs):
        """把可见区域画到画布上

        做法：为每个可见像元生成一个小方块，全部合并成一个多边形。
        合并后边界连续，不会出现条纹接缝。
        """
        ext = result['extent']
        px_x, px_y = result['px']
        grid = result['grid']
        cols, rows = result['cols'], result['rows']

        from qgis.core import QgsCoordinateTransform, QgsGeometry as G, QgsPointXY as P
        tr = None
        if dem_crs != canvas_crs:
            tr = QgsCoordinateTransform(dem_crs, canvas_crs, QgsProject.instance())

        def conv(x, y):
            if tr is None:
                return P(x, y)
            p = tr.transform(P(x, y))
            return P(p.x(), p.y())

        # 逐像元生成方块，收集所有角点后一次性合并
        polys = []
        for ri in range(rows):
            row = grid[ri]
            for ci in range(cols):
                if row[ci] != 1:
                    continue
                x0 = ext.xMinimum() + ci * px_x
                x1 = x0 + px_x
                y1 = ext.yMaximum() - ri * px_y
                y0 = y1 - px_y
                polys.append([conv(x0, y0), conv(x1, y0),
                              conv(x1, y1), conv(x0, y1)])

        if not polys:
            self.result_rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
            self.canvas.refresh()
            return

        # 逐块加入橡皮筋：它内部会把它们合成一个 QgsGeometryCollection，
        # 渲染时共边自然贴合，不会出现条纹接缝。
        self.result_rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        for ring in polys:
            self.result_rb.addGeometry(G.fromPolygonXY([ring]), None)
        self.result_rb.show()
        self.canvas.refresh()

    def _finish(self):
        self._toast.clear()
        self.canvas.unsetMapTool(self)


class _Toast(object):
    def __init__(self, iface):
        self.iface = iface

    def hint(self, text, duration=4):
        if self.iface is None:
            return
        try:
            self.iface.messageBar().pushMessage('视域分析', text, level=0, duration=duration)
        except (AttributeError, TypeError):
            pass

    def clear(self):
        pass
