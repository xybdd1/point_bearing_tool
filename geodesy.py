# -*- coding: utf-8 -*-
"""
geodesy.py - 距离与方位角计算

设计要点：
1. 距离用 QgsDistanceArea 在 WGS84 椭球面上计算（measureLine），
   不自己写 haversine——椭球面才和 QGIS 自带测量工具一致。
2. 方位角用 QgsDistanceArea.bearing()，它返回的是【弧度】不是度，
   而且向西会给出负值（例如正西 = -1.5708），必须归一化到 [0, 360)。
3. 方位角是"起点的大圆初始方位角"：从 A 出发沿大圆走向 B 时的初始航向，
   以真北为 0°、顺时针增加。这与导航/通信里说的方位角含义一致。
"""

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsPointXY,
    QgsProject,
    QgsUnitTypes,
)

WGS84 = 'EPSG:4326'

# 中文方位描述，按 22.5° 一个扇区（共 16 个）
#
# 命名规则：四个正方向用单字（北/东/南/西），四个斜方向用"东北"这类组合词，
# 中间那 8 个方向用"从主方向偏向次方向"的说法，例如 112.5° 是「东偏南」
# （正东往南偏 22.5°）。
#
# 不用英文的增减译法（如 "east-southeast" → 东东南），那种说法中文里不自然。
_COMPASS_16 = (
    '北',    #   0.0°  北偏东   22.5°
    '北偏东',
    '东北',  #  45.0°  东偏北   67.5°
    '东偏北',
    '东',    #  90.0°  东偏南  112.5°
    '东偏南',
    '东南',  # 135.0°  南偏东  157.5°
    '南偏东',
    '南',    # 180.0°  南偏西  202.5°
    '南偏西',
    '西南',  # 225.0°  西偏南  247.5°
    '西偏南',
    '西',    # 270.0°  西偏北  292.5°
    '西偏北',
    '西北',  # 315.0°  北偏西  337.5°
    '北偏西',
)


def make_distance_area(crs_authid=WGS84, ellipsoid='WGS84'):
    """构造在指定地理坐标系上做椭球计算的 QgsDistanceArea"""
    area = QgsDistanceArea()
    area.setSourceCrs(QgsCoordinateReferenceSystem(crs_authid),
                      QgsProject.instance().transformContext())
    area.setEllipsoid(ellipsoid)
    return area


def normalize_bearing(degrees):
    """把任意角度归一化到 [0, 360)"""
    return degrees % 360.0


def compass_label(bearing_deg):
    """方位角 -> 16 方位中文描述"""
    index = int((normalize_bearing(bearing_deg) + 11.25) // 22.5) % 16
    return _COMPASS_16[index]


def initial_bearing(point_a, point_b):
    """计算 A 到 B 的大圆初始方位角（度，真北为 0，顺时针）

    优先用 QgsDistanceArea.bearing()（椭球面），失败时回退到球面公式。
    注意 bearing() 返回弧度且可能为负，这里统一转成 [0, 360) 的度。
    """
    area = make_distance_area()
    try:
        radians = area.bearing(point_a, point_b)
        return normalize_bearing(math.degrees(radians))
    except Exception:
        return normalize_bearing(_spherical_bearing(point_a, point_b))


def _spherical_bearing(point_a, point_b):
    """球面大圆初始方位角（度），作为 bearing() 不可用时的后备"""
    lat1 = math.radians(point_a.y())
    lon1 = math.radians(point_a.x())
    lat2 = math.radians(point_b.y())
    lon2 = math.radians(point_b.x())

    delta_lon = lon2 - lon1
    y = math.sin(delta_lon) * math.cos(lat2)
    x = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon))
    if x == 0 and y == 0:
        return 0.0
    return math.degrees(math.atan2(y, x))


def reverse_bearing(point_a, point_b):
    """B 到 A 的方位角（终点反方位角）"""
    return initial_bearing(point_b, point_a)


def distance_meters(point_a, point_b, crs_authid=WGS84, ellipsoid='WGS84'):
    """两点椭球面距离（米）"""
    area = make_distance_area(crs_authid, ellipsoid)
    return area.measureLine(point_a, point_b)


def format_distance(meters):
    """按数量级自动选择可读单位"""
    if meters < 1000:
        return f'{meters:.1f} 米'
    if meters < 1_000_000:
        return f'{meters / 1000.0:.3f} 公里'
    return f'{meters / 1_000_000.0:.3f} 千公里'


def convert_distance(meters, unit_code):
    """把米换算成指定单位"""
    area = make_distance_area()
    return area.convertLengthMeasurement(meters, unit_code)


DISTANCE_UNITS = (
    ('米', QgsUnitTypes.DistanceMeters),
    ('公里', QgsUnitTypes.DistanceKilometers),
    ('英尺', QgsUnitTypes.DistanceFeet),
    ('海里', QgsUnitTypes.DistanceNauticalMiles),
)


def to_wgs84(point, source_crs):
    """把画布坐标转换到 WGS84 经纬度"""
    target = QgsCoordinateReferenceSystem(WGS84)
    if source_crs is None or source_crs == target:
        return QgsPointXY(point)

    transform = QgsCoordinateTransform(source_crs, target, QgsProject.instance())
    converted = transform.transform(QgsPointXY(point))
    return QgsPointXY(converted.x(), converted.y())


class Measurement(object):
    """一次两点测量的完整结果"""

    def __init__(self, point_a, point_b, crs_authid=WGS84, ellipsoid='WGS84'):
        self.point_a = QgsPointXY(point_a)
        self.point_b = QgsPointXY(point_b)
        self.crs_authid = crs_authid

        self.distance_m = distance_meters(self.point_a, self.point_b,
                                          crs_authid, ellipsoid)

        # 两点完全重合时，bearing() 会返回 180°，这对用户没有意义，
        # 标记为 None，界面上显示为 "—"。
        self.is_degenerate = self.distance_m == 0.0
        if self.is_degenerate:
            self.bearing_deg = None
            self.reverse_bearing_deg = None
        else:
            self.bearing_deg = initial_bearing(self.point_a, self.point_b)
            self.reverse_bearing_deg = reverse_bearing(self.point_a, self.point_b)

        # 经纬度差（度）
        self.delta_lon = self.point_b.x() - self.point_a.x()
        self.delta_lat = self.point_b.y() - self.point_a.y()

        # 大致坡度角：用距离与高差（本工具无高程，留 0）
        self.slope_deg = 0.0

    @property
    def compass(self):
        if self.bearing_deg is None:
            return '—'
        return compass_label(self.bearing_deg)

    @property
    def bearing_text(self):
        if self.bearing_deg is None:
            return '—'
        return f'{self.bearing_deg:.3f}°'

    @property
    def reverse_bearing_text(self):
        if self.reverse_bearing_deg is None:
            return '—'
        return f'{self.reverse_bearing_deg:.3f}°'

    @property
    def distance_text(self):
        return format_distance(self.distance_m)

    def summary(self):
        """多行文本汇总，用于面板显示"""
        if self.is_degenerate:
            bearing_line = '方位角：—（两点重合）\n反方位角：—'
        else:
            bearing_line = (f'方位角：{self.bearing_text}  ({self.compass})\n'
                            f'反方位角：{self.reverse_bearing_text}')
        return (
            f'A：{self.point_a.x():.6f}, {self.point_a.y():.6f}\n'
            f'B：{self.point_b.x():.6f}, {self.point_b.y():.6f}\n'
            f'\n'
            f'距离：{self.distance_text}\n'
            f'{bearing_line}\n'
            f'\n'
            f'经度差：{self.delta_lon:+.6f}°\n'
            f'纬度差：{self.delta_lat:+.6f}°'
        )
