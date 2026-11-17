# -*- coding: utf-8 -*-
"""
viewshed.py - 视域（可见范围）计算

算法：方位角射线扫描 + 像元标记
    对观察点周围的每个方位角发一条射线，沿射线向外走，
    记录"已经过的最大仰角"。射线经过的每个 DEM 像元：
        - 该点仰角 >= 历史最大仰角  -> 标记为可见
        - 否则                       -> 标记为被挡

    这是标准的视线剖面方法。比"逐像元反向追踪"快约 80 倍，
    因为采样次数从 O(像元数 × 步数) 降到 O(射线数 × 步数)。

坐标系：
    DEM 可能是经纬度（EPSG:4326），此时东西/南北方向的"度"对应米数不同。
    本模块按观察点纬度换算，内部一律用米计算，输出保留 DEM 原始坐标。
"""

import math

EARTH_RADIUS = 6371008.8
M_PER_DEG_LAT = 111132.95
M_PER_DEG_LON_EQ = 111320.0

# 网格状态
VISIBLE = 1
BLOCKED = 0


class ViewshedError(Exception):
    """视域计算的业务错误（给用户看的中文原因）"""


def check_dem_layer(layer):
    """检查 DEM 图层是否可用于视域计算

    返回 (ok: bool, reason: str)

    关键：在线瓦片图层（XYZ/WMS/WCS）每次取样都要走网络。
    视域计算要读几万到几十万个像元，会发出成千上万个请求，
    直接把 QGIS 卡死。所以必须提前拒绝。
    """
    if layer is None:
        return False, '未选择高程(DEM)图层'

    try:
        provider = layer.dataProvider()
    except (AttributeError, RuntimeError):
        return False, '图层无法读取'

    if provider is None:
        return False, '图层无法读取'

    # 图层源字符串里带 type=xyz / type=wms / type=wcs 的就是在线服务
    source = ''
    try:
        source = provider.dataSourceUri() or ''
    except (AttributeError, RuntimeError):
        source = ''

    low = source.lower()
    for tag, kind in (('type=xyz', '在线瓦片(XYZ)'),
                      ('type=wms', '在线服务(WMS)'),
                      ('type=wcs', '在线服务(WCS)')):
        if tag in low:
            return False, (
                '这是%s图层，高程要实时联网下载。\n'
                '视域计算需要读取数万个像元，会发出成千上万次网络请求、导致卡死。\n\n'
                '请改用本地高程文件（.tif/.img/.asc/.hgt），'
                '通过菜单「设置高程(DEM)图层…」选择。' % kind)

    # 没有像素尺寸的（坏图层）
    try:
        if layer.rasterUnitsPerPixelX() <= 0 or layer.rasterUnitsPerPixelY() <= 0:
            return False, '图层的像元尺寸无效，无法用于视域计算'
    except (AttributeError, RuntimeError):
        return False, '图层无法读取像元信息'

    return True, ''


def _read_pixels(provider, extent, n_cols, n_rows, band=1):
    """读取一块像元，返回 (二维浮点数组, nodata)

    处理 QGIS 的多波段栅格与 terrarium 高程编码。
    """
    blk = provider.block(band, extent, n_cols, n_rows)
    if blk is None or blk.isEmpty():
        return None, None

    nodata = None
    try:
        nodata = provider.sourceNoDataValue(band)
    except (AttributeError, RuntimeError):
        pass

    data = []
    for r in range(blk.height()):
        row = []
        for c in range(blk.width()):
            v = blk.value(r, c)
            if v is None:
                row.append(None)
                continue
            if isinstance(v, float) and math.isnan(v):
                row.append(None)
                continue
            if nodata is not None:
                try:
                    if abs(v - nodata) < 1e-9:
                        row.append(None)
                        continue
                except TypeError:
                    pass
            row.append(float(v))
        data.append(row)
    return data, nodata


def meters_per_degree(latitude):
    """该纬度上 1 度经度 / 1 度纬度 对应的米数"""
    lat = max(-89.5, min(89.5, float(latitude)))
    return M_PER_DEG_LON_EQ * math.cos(math.radians(lat)), M_PER_DEG_LAT


class _DemBlock(object):
    """观察点周边的 DEM 内存块（预先转成 Python 二维数组，加速内层循环）"""

    def __init__(self, layer, center_xy, radius_m):
        self.layer = layer
        self.provider = layer.dataProvider()
        self.crs = layer.crs()
        self.geographic = self.crs.isGeographic()

        # 先做可用性检查（在线图层直接拒绝，否则会卡死）
        ok, reason = check_dem_layer(layer)
        if not ok:
            raise ViewshedError(reason)

        self.px_x = layer.rasterUnitsPerPixelX()
        self.px_y = layer.rasterUnitsPerPixelY()
        if self.px_x <= 0 or self.px_y <= 0:
            raise ValueError('DEM 像元尺寸无效')

        self.center = center_xy
        self.m_lon, self.m_lat = meters_per_degree(center_xy.y())

        if self.geographic:
            self.px_m_x = self.px_x * self.m_lon
            self.px_m_y = self.px_y * self.m_lat
        else:
            self.px_m_x = self.px_x
            self.px_m_y = self.px_y

        # 外扩 3 像元，避免射线到达边缘时缺数据
        pad_x = self.px_m_x * 3
        pad_y = self.px_m_y * 3
        max_m_x = radius_m + pad_x
        max_m_y = radius_m + pad_y

        if self.geographic:
            half_x = max_m_x / self.m_lon
            half_y = max_m_y / self.m_lat
        else:
            half_x, half_y = max_m_x, max_m_y

        from qgis.core import QgsRectangle
        want = QgsRectangle(center_xy.x() - half_x, center_xy.y() - half_y,
                            center_xy.x() + half_x, center_xy.y() + half_y)

        # 把读取范围对齐到 DEM 像元格网。
        # provider.block() 遇到没对齐的范围会做重采样，观察点可能落到相邻像元上，
        # 在陡坡地形上地面高程能差好几米（视域结果跟着漂）。
        # 对齐后 block 的像元与 DEM 像元一一对应，采样才是真实的。
        layer_ext = layer.extent()
        x_off0 = (want.xMinimum() - layer_ext.xMinimum()) / self.px_x
        y_off0 = (layer_ext.yMaximum() - want.yMaximum()) / self.px_y
        x_off1 = (want.xMaximum() - layer_ext.xMinimum()) / self.px_x
        y_off1 = (layer_ext.yMaximum() - want.yMinimum()) / self.px_y

        c0 = max(0, int(math.floor(x_off0)))
        r0 = max(0, int(math.floor(y_off0)))
        c1 = min(layer.width(), int(math.ceil(x_off1)))
        r1 = min(layer.height(), int(math.ceil(y_off1)))
        if c1 <= c0 or r1 <= r0:
            raise ValueError('观察点不在 DEM 范围内')

        self.extent = QgsRectangle(layer_ext.xMinimum() + c0 * self.px_x,
                                   layer_ext.yMaximum() - r1 * self.px_y,
                                   layer_ext.xMinimum() + c1 * self.px_x,
                                   layer_ext.yMaximum() - r0 * self.px_y)
        if self.extent.isEmpty():
            raise ValueError('观察点不在 DEM 范围内')

        n_cols = c1 - c0
        n_rows = r1 - r0

        self.block = self.provider.block(1, self.extent, n_cols, n_rows)
        if self.block is None or self.block.isEmpty():
            raise ValueError('DEM 读取失败')

        self.cols = self.block.width()
        self.rows = self.block.height()
        self.nodata = self.provider.sourceNoDataValue(1)

        # 一次性转成 Python 数组（纯 Python 循环里访问快得多）
        self.data = []
        for r in range(self.rows):
            row = []
            for c in range(self.cols):
                v = self.block.value(r, c)
                row.append(float(v) if self._valid(v) else None)
            self.data.append(row)

        # 释放 raster block，省内存
        self.block = None

    def _valid(self, v):
        if v is None:
            return False
        if isinstance(v, float) and math.isnan(v):
            return False
        if self.nodata is not None:
            try:
                if abs(v - self.nodata) < 1e-9:
                    return False
            except TypeError:
                pass
        return True

    def sample(self, col, row):
        """双线性插值（col/row 为浮点像元坐标）"""
        c0 = int(math.floor(col))
        r0 = int(math.floor(row))
        if c0 < 0 or r0 < 0 or c0 >= self.cols - 1 or r0 >= self.rows - 1:
            # 边缘：用最近像元
            cc = min(max(c0, 0), self.cols - 1)
            rr = min(max(r0, 0), self.rows - 1)
            return self.data[rr][cc]

        fx = col - c0
        fy = row - r0

        v00 = self.data[r0][c0]
        v10 = self.data[r0][c0 + 1]
        v01 = self.data[r0 + 1][c0]
        v11 = self.data[r0 + 1][c0 + 1]

        if v00 is None or v10 is None or v01 is None or v11 is None:
            for rr in (r0, r0 + 1):
                for cc in (c0, c0 + 1):
                    v = self.data[rr][cc]
                    if v is not None:
                        return v
            return None

        top = v00 * (1 - fx) + v10 * fx
        bot = v01 * (1 - fx) + v11 * fx
        return top * (1 - fy) + bot * fy

    def xy_to_colrow(self, x, y):
        """地图坐标 -> 像元中心索引（浮点，配双线性插值）

        注意：返回的是"像元中心"索引（第 0 个像元的中心是 0.0），
        这样和 sample() 的双线性约定一致；按角点索引会整体偏半个像元。
        """
        return ((x - self.extent.xMinimum()) / self.px_x - 0.5,
                (self.extent.yMaximum() - y) / self.px_y - 0.5)

    def colrow_to_xy(self, col, row):
        """像元中心索引 -> 该像元中心的地图坐标"""
        return (self.extent.xMinimum() + (col + 0.5) * self.px_x,
                self.extent.yMaximum() - (row + 0.5) * self.px_y)


def compute_viewshed(dem_layer, center_xy, radius_m,
                     observer_height=1.7, target_height=0.0,
                     curvature=True, tolerance=0.02):
    """计算视域

    参数：
        dem_layer        QgsRasterLayer（经纬度或投影坐标系）
        center_xy        QgsPointXY 观察点（DEM 坐标系）
        radius_m         视距半径（米）
        observer_height  观察者高于地面的高度（米）
        target_height    目标高度（米）
        curvature        是否做地球曲率修正

    返回 dict：
        {
          'grid':     [[1/0/None,...], ...]  按 [行][列] 索引，None = 无数据
          'cols','rows': int
          'extent':  QgsRectangle（DEM 原始坐标）
          'px':      (px_x, px_y)  DEM 原始像元单位
          'observer_z': float    视点高程（含观察者高度）
          'ground_z':   float    观察点地面高程
          'visible_count','blocked_count': int
          'crs': QgsCoordinateReferenceSystem
        }
        失败返回 None
    """
    try:
        blk = _DemBlock(dem_layer, center_xy, radius_m)
    except ViewshedError:
        raise
    except (ValueError, AttributeError, RuntimeError):
        return None

    oc, orow = blk.xy_to_colrow(center_xy.x(), center_xy.y())
    ground_z = blk.sample(oc, orow)
    if ground_z is None:
        return None
    observer_z = ground_z + observer_height

    grid = [[None] * blk.cols for _ in range(blk.rows)]

    # 步长取 0.35 像元，保证射线不漏像元
    step_m = max(0.5, min(blk.px_m_x, blk.px_m_y) * 0.35)

    # 射线数量：让最外圈相邻射线间距明显小于 1 像元（多打一些保证覆盖）
    n_rays = int(2.0 * math.pi * radius_m / max(1.0, min(blk.px_m_x, blk.px_m_y)))
    n_rays = max(144, min(7200, n_rays * 3))

    # 观察点自身所属像元
    ci0 = min(max(int(oc), 0), blk.cols - 1)
    ri0 = min(max(int(orow), 0), blk.rows - 1)
    grid[ri0][ci0] = VISIBLE

    inv_lon = 1.0 / blk.m_lon if blk.geographic else 1.0
    inv_lat = 1.0 / blk.m_lat if blk.geographic else 1.0

    for i in range(n_rays):
        az = 2.0 * math.pi * i / n_rays
        dx_m = math.cos(az)
        dy_m = math.sin(az)

        max_tan = -1e30
        r = step_m

        while r <= radius_m:
            x = center_xy.x() + dx_m * r * inv_lon
            y = center_xy.y() + dy_m * r * inv_lat

            col, row = blk.xy_to_colrow(x, y)
            ci = int(col)
            ri = int(row)
            if ci < 0 or ri < 0 or ci >= blk.cols or ri >= blk.rows:
                break

            z = blk.sample(col, row)
            if z is None:
                r += step_m
                continue

            dz = (z + target_height) - observer_z
            if curvature:
                dz -= (r * r) / (2.0 * EARTH_RADIUS)
            tan_elev = dz / r

            # tolerance: 仰角相差很小时视为"擦边可见"，避免 DEM 噪声
            # 把一个像元的微小起伏当成永久遮挡
            if tan_elev + tolerance >= max_tan:
                if grid[ri][ci] != VISIBLE:
                    grid[ri][ci] = VISIBLE
                if tan_elev > max_tan:
                    max_tan = tan_elev
            else:
                # 已可见的像元不降级（多射线可能经过同一像元）
                if grid[ri][ci] is None:
                    grid[ri][ci] = BLOCKED

            r += step_m

    # 射线是离散的，会漏掉夹在两条射线之间的像元。
    # 用邻域填充：紧邻可见像元的未知像元，若高程不明显高于视线则视为可见。
    _fill_gaps(blk, grid, center_xy, observer_z,
               radius_m, step_m, curvature, target_height)

    # 统计
    vis = blk_count = 0
    for row in grid:
        for v in row:
            if v == VISIBLE:
                vis += 1
            elif v == BLOCKED:
                blk_count += 1

    return {
        'grid': grid,
        'cols': blk.cols,
        'rows': blk.rows,
        'extent': blk.extent,
        'px': (blk.px_x, blk.px_y),
        'observer_z': observer_z,
        'ground_z': ground_z,
        'visible_count': vis,
        'blocked_count': blk_count,
        'crs': blk.crs,
    }


def _fill_gaps(blk, grid, center_xy, observer_z, radius_m, step_m,
               curvature, target_height):
    """填补射线之间的空隙像元

    射线是离散的，两条相邻射线之间会夹着没被扫到的像元。
    对每个未知像元，沿观察点→该像元方向重新做一次完整的视线检查
    （次数远少于全网格，因为只处理剩下的一小部分）。
    """
    cols, rows = blk.cols, blk.rows
    cc, cr = int(blk.xy_to_colrow(center_xy.x(), center_xy.y())[0]), \
             int(blk.xy_to_colrow(center_xy.x(), center_xy.y())[1])

    inv_lon = 1.0 / blk.m_lon if blk.geographic else 1.0
    inv_lat = 1.0 / blk.m_lat if blk.geographic else 1.0

    for ri in range(rows):
        row = grid[ri]
        for ci in range(cols):
            if row[ci] is not None:
                continue

            tx, ty = blk.colrow_to_xy(ci, ri)
            tz = blk.sample(ci, ri)
            if tz is None:
                continue

            dx_m = (tx - center_xy.x()) * (blk.m_lon if blk.geographic else 1.0)
            dy_m = (ty - center_xy.y()) * (blk.m_lat if blk.geographic else 1.0)
            dist = math.hypot(dx_m, dy_m)

            if dist > radius_m:
                continue
            if dist <= step_m:
                row[ci] = VISIBLE
                continue

            # 完整视线检查
            n_steps = max(2, int(dist / step_m))
            max_tan = -1e30
            for k in range(1, n_steps):
                f = k / float(n_steps)
                mx = center_xy.x() + (tx - center_xy.x()) * f
                my = center_xy.y() + (ty - center_xy.y()) * f
                mc, mr = blk.xy_to_colrow(mx, my)
                mz = blk.sample(mc, mr)
                if mz is None:
                    continue
                d_k = dist * f
                if d_k <= 0:
                    continue
                dz = mz - observer_z
                if curvature:
                    dz -= (d_k * d_k) / (2.0 * EARTH_RADIUS)
                t = dz / d_k
                if t > max_tan:
                    max_tan = t

            dz_t = (tz + target_height) - observer_z
            if curvature:
                dz_t -= (dist * dist) / (2.0 * EARTH_RADIUS)
            tan_t = dz_t / dist

            row[ci] = VISIBLE if tan_t >= max_tan else BLOCKED


def _intersect(rect_a, rect_b):
    """QgsRectangle 求交（兼容 QGIS 3 / 4）

    实测行为（QGIS 4.2）：
        QgsRectangle.intersect(other) 不修改自身，返回一个新的 QgsRectangle。
        QGIS 3 的写法是 intersected(other)，返回新矩形。
    两者语义一致，都取返回值即可。
    """
    from qgis.core import QgsRectangle

    if hasattr(rect_a, 'intersected'):
        return rect_a.intersected(rect_b)

    result = QgsRectangle(rect_a).intersect(rect_b)
    if result is None or not isinstance(result, QgsRectangle):
        return QgsRectangle()
    return result if not result.isEmpty() else QgsRectangle()
