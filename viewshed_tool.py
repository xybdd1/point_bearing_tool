# -*- coding: utf-8 -*-
"""
viewshed_tool.py - 视域分析地图工具

交互（和范围圈工具一致）：
    左键点一下  -> 定观察点（圆心）
    移动鼠标    -> 半径跟着走，松手前实时显示范围
    左键再点    -> 按当前半径计算视域
    右键 / Esc  -> 结束，保留结果

视域计算依赖 DEM 高程图层；DEM 只需在首次使用时选一次，之后记住。

DEM 支持两种来源：
    本地文件（.tif/.img/...）——直接读像元
    在线 Terrarium XYZ 高程（如 terrarium.nextgis.ru）——先把视距圆覆盖的
        少量瓦片下载到本地缓存、拼成临时 GeoTIFF，再走同一套算法
"""

import collections
import hashlib
import os

from qgis.core import (QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer,
                       QgsRectangle, QgsWkbTypes)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import QCoreApplication, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

from . import geodesy
from . import online_dem
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
        self._busy = False
        # 同一位置、同一视距重复计算时直接复用（LRU，最多 6 组）
        self._result_cache = collections.OrderedDict()
        self._drawn_key = None

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

        # 结果图层（可见区域）：只填充、不描边
        # 相邻像元共用边，QPainter 奇偶填充会自动合成一整块纯色区域；
        # 一旦给边描线，就会画出一格一格的网格（看起来像栅格网格）。
        self.result_rb = QgsRubberBand(self.canvas,
                                       QgsWkbTypes.GeometryType.PolygonGeometry)
        self.result_rb.setColor(QColor(60, 220, 100, 80))
        if hasattr(self.result_rb, 'setStrokeColor'):
            self.result_rb.setStrokeColor(QColor(60, 220, 100, 0))
        self.result_rb.setWidth(0)

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
        self._drawn_key = None
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
        if self._busy:
            return
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
        if self._busy:
            return
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
        if self._busy:
            return
        if self.center is None or self.radius_m <= 0:
            self._toast.hint('请先移动鼠标确定视距')
            return

        dem = self._dem_layer()
        if dem is None:
            self.analysisFailed.emit('未选择 DEM 高程图层')
            return

        # 在线 Terrarium 高程：先下载/复用瓦片缓存、拼成本地临时 GeoTIFF，
        # 之后和本地 DEM 走完全相同的算法
        if online_dem.layer_is_terrarium(dem):
            dem = self._prepare_online_dem(dem)
            if dem is None:
                return

        # 其他在线图层 / 坏图层提前拦截，避免读几万像元把 QGIS 卡死
        ok, reason = vs_mod.check_dem_layer(dem)
        if not ok:
            self._toast.hint(reason.replace('\n', ' '), 10)
            self.analysisFailed.emit(reason)
            self._show_error(reason)
            return

        cfg = self.settings

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

            # 同一位置、同一视距、同一参数：直接复用上次结果
            key = self._result_key(dem, center_dem)
            cached = self._get_cached_result(key)
            if cached is not None:
                if self._drawn_key != key:
                    self._draw_result(cached, dem_crs, canvas_crs)
                    self._drawn_key = key
                self.locked = True
                vis, blk = cached['visible_count'], cached['blocked_count']
                tot = vis + blk
                pct = vis / tot * 100 if tot else 0
                self._toast.hint(
                    '缓存命中：可见 %.1f%%（%d/%d 像元）  地面 %.0fm（观察高 %.1fm）  '
                    '左键重算，右键结束'
                    % (pct, vis, tot, cached['ground_z'],
                       cached['observer_z'] - cached['ground_z']), 8)
                self.analysisDone.emit(cached)
                return

            self._toast.hint('正在计算视域…')
            self.analysisStarted.emit()

            result = vs_mod.compute_viewshed(
                dem, center_dem, self.radius_m,
                observer_height=cfg.observer_height,
                target_height=cfg.target_height,
                tolerance=cfg.tolerance)

            if result is None:
                self.analysisFailed.emit('计算失败：观察点可能不在 DEM 覆盖范围内')
                self._toast.hint('计算失败：观察点可能不在 DEM 覆盖范围内', 8)
                return

            self._remember_result(key, result)
            self._save_result_to_disk(key, result)

            self._draw_result(result, dem_crs, canvas_crs)
            self._drawn_key = key
            self.locked = True

            vis, blk = result['visible_count'], result['blocked_count']
            tot = vis + blk
            pct = vis / tot * 100 if tot else 0
            self._toast.hint(
                '可见 %.1f%%（%d/%d 像元）  地面 %.0fm（观察高 %.1fm）  左键重算，右键结束'
                % (pct, vis, tot, result['ground_z'],
                   result['observer_z'] - result['ground_z']), 8)
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

    def _prepare_online_dem(self, layer):
        """在线 Terrarium 图层 -> 本地缓存 GeoTIFF 图层（失败返回 None）"""
        if self._busy:
            return None
        try:
            source = layer.dataProvider().dataSourceUri() or ''
        except (AttributeError, RuntimeError, TypeError):
            source = ''

        lonlat = self._to_wgs84(self.center)
        self._busy = True
        try:
            def _progress(phase, done, total):
                if phase == 'download':
                    self._toast.hint('正在下载在线高程瓦片 %d/%d …'
                                     % (done, total), 1)
                else:
                    self._toast.hint('正在拼接高程 DEM …', 1)
                try:
                    QCoreApplication.processEvents()
                except (AttributeError, RuntimeError):
                    pass

            meta = online_dem.build_dem_for_point(
                source, lonlat.x(), lonlat.y(), self.radius_m,
                progress=_progress)

            local = QgsRasterLayer(meta['path'], '在线高程 DEM', 'gdal')
            if not local.isValid():
                raise online_dem.OnlineDemError(
                    '在线高程 DEM 打开失败：%s' % meta['path'])

            if meta['reused']:
                self._toast.hint('在线高程：命中本地 DEM 缓存（z%d），开始计算'
                                 % meta['zoom'], 3)
            else:
                self._toast.hint(
                    '在线高程就绪：z%d / %d 块瓦片（下载 %d 块）  %.1f 秒'
                    % (meta['zoom'], meta['tiles'], meta['downloaded'],
                       meta['elapsed']), 5)
            return local
        except online_dem.OnlineDemError as exc:
            message = str(exc)
            self._toast.hint(message.replace('\n', ' '), 10)
            self.analysisFailed.emit(message)
            self._show_error(message)
            return None
        except Exception as exc:  # noqa: BLE001 - 兜底给用户可读提示
            import traceback
            traceback.print_exc()
            message = '在线高程处理失败：%s' % exc
            self._toast.hint(message, 10)
            self.analysisFailed.emit(message)
            self._show_error(message)
            return None
        finally:
            self._busy = False

    def _result_key(self, dem, center_dem):
        """结果缓存键：DEM 来源/修改时间 + 观察点 + 视距 + 参数"""
        try:
            source = dem.source() or ''
            mtime = int(os.path.getmtime(source)) if os.path.exists(source) else 0
        except (OSError, RuntimeError, TypeError):
            source, mtime = '', 0
        cfg = self.settings
        return (source, mtime,
                round(float(center_dem.x()), 7), round(float(center_dem.y()), 7),
                round(float(self.radius_m), 2),
                cfg.observer_height, cfg.target_height, cfg.tolerance)

    def _remember_result(self, key, result):
        self._result_cache[key] = result
        while len(self._result_cache) > 6:
            self._result_cache.popitem(last=False)

    def _get_cached_result(self, key):
        """两级结果缓存：内存 LRU -> 磁盘（重启 QGIS 后仍可秒出）"""
        cached = self._result_cache.pop(key, None)
        if cached is not None:
            self._result_cache[key] = cached
            return cached
        cached = self._load_result_from_disk(key)
        if cached is not None:
            self._remember_result(key, cached)
        return cached

    def _result_cache_file(self, key):
        try:
            cache_dir = online_dem.resolve_cache_dir()
        except online_dem.OnlineDemError:
            return None
        digest = hashlib.sha1(repr(key).encode('utf-8')).hexdigest()[:20]
        return os.path.join(cache_dir, 'viewshed_cache', digest + '.npz')

    def _load_result_from_disk(self, key):
        path = self._result_cache_file(key)
        if not path or not os.path.exists(path):
            return None
        try:
            import numpy as np
            data = np.load(path)
            grid = [[None if int(v) == 2 else int(v) for v in row]
                    for row in data['grid']]
            ext = data['extent']
            return {
                'grid': grid,
                'cols': int(data['cols']),
                'rows': int(data['rows']),
                'extent': QgsRectangle(float(ext[0]), float(ext[1]),
                                       float(ext[2]), float(ext[3])),
                'px': (float(data['px_x']), float(data['px_y'])),
                'observer_z': float(data['observer_z']),
                'ground_z': float(data['ground_z']),
                'visible_count': int(data['visible_count']),
                'blocked_count': int(data['blocked_count']),
                'crs': None,
            }
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _save_result_to_disk(self, key, result):
        path = self._result_cache_file(key)
        if not path:
            return
        try:
            import numpy as np
            os.makedirs(os.path.dirname(path), exist_ok=True)
            cols, rows = int(result['cols']), int(result['rows'])
            flat = [1 if v == 1 else (0 if v == 0 else 2)
                    for row in result['grid'] for v in row]
            grid_ids = np.array(flat, dtype=np.int8).reshape(rows, cols)
            ext = result['extent']
            tmp = path + '.tmp.npz'
            np.savez_compressed(
                tmp, grid=grid_ids, cols=cols, rows=rows,
                extent=np.array([ext.xMinimum(), ext.yMinimum(),
                                 ext.xMaximum(), ext.yMaximum()]),
                px_x=result['px'][0], px_y=result['px'][1],
                observer_z=result['observer_z'], ground_z=result['ground_z'],
                visible_count=result['visible_count'],
                blocked_count=result['blocked_count'])
            os.replace(tmp, path)
        except (OSError, ValueError, TypeError):
            pass

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

        做法：把所有可见像元拼成一个 MultiPolygon，整体一次坐标变换后交给
        橡皮筋。相邻像元共边、填充用奇偶规则，会合成一整块纯色区域；
        橡皮筋不描边（见 __init__），所以不会出现一格一格的网格线。
        """
        ext = result['extent']
        px_x, px_y = result['px']
        grid = result['grid']
        cols, rows = result['cols'], result['rows']

        rings = []
        for ri in range(rows):
            row = grid[ri]
            for ci in range(cols):
                if row[ci] != 1:
                    continue
                x0 = ext.xMinimum() + ci * px_x
                x1 = x0 + px_x
                y1 = ext.yMaximum() - ri * px_y
                y0 = y1 - px_y
                rings.append([QgsPointXY(x0, y0), QgsPointXY(x1, y0),
                              QgsPointXY(x1, y1), QgsPointXY(x0, y1)])

        if not rings:
            self.result_rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
            self.canvas.refresh()
            return

        from qgis.core import QgsCoordinateTransform
        geometry = QgsGeometry.fromMultiPolygonXY([[ring] for ring in rings])
        if dem_crs != canvas_crs:
            tr = QgsCoordinateTransform(dem_crs, canvas_crs, QgsProject.instance())
            geometry.transform(tr)
        if geometry.isEmpty():
            self.result_rb.reset(QgsWkbTypes.GeometryType.UnknownGeometry)
            self.canvas.refresh()
            return

        self.result_rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self.result_rb.addGeometry(geometry, None)
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
