# -*- coding: utf-8 -*-
"""验收测试：在线高程视域分析（无头 QGIS）

用法（QGIS 自带的 Python）：
    "D:\\Program Files\\QGIS 4.2.0\\bin\\python-qgis.bat" dev_tests\\acceptance_online_dem.py

可选环境变量：
    PB_REPO                  被测插件目录（默认=本仓库根目录）
    PB_PKG                   包名（默认=目录名）
    PB_LOCAL_DEM             本地参照 DEM（默认 合并.tif）
    POINT_BEARING_CACHE_DIR  缓存目录（默认 D:\\Temp\\point_bearing_cache）

覆盖：
  A. 单元：源识别 / 瓦片数学 / Terrarium 解码已知答案
  B. 冷启动：清空缓存 -> 下载 + 拼 DEM + 视域计算
  D. 只清 DEM：瓦片还在 -> 不重新下载，重拼
  E. 断网：坏地址 -> 快速报错，不卡死
  F. 与本地 合并.tif 交叉验证：可见比例 / IoU / 地面高程
  G. 视域工具端到端：首次含下载 <5s；同点第二次 <1s（内存/磁盘缓存）
  H. 本地 DEM 回归：视域工具路径正常
"""
import gc
import importlib.util
import math
import os
import shutil
import sys
import time

import numpy as np

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from osgeo import gdal

gdal.UseExceptions()

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform, QgsPointXY, QgsProject,
                       QgsRasterLayer, QgsRectangle)
from qgis.gui import QgsMapCanvas

REPO = (os.environ.get('PB_REPO')
        or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = os.environ.get('PB_PKG', os.path.basename(REPO))
sys.path.insert(0, os.path.dirname(REPO))

online_dem = importlib.import_module(PKG + '.online_dem')
vt = importlib.import_module(PKG + '.viewshed_tool')

CACHE = os.environ.get('POINT_BEARING_CACHE_DIR', r'D:\Temp\point_bearing_cache')
LOCAL_TIF = os.environ.get('PB_LOCAL_DEM',
                           r'C:\Users\xuyib\Desktop\图层\高程\合并\合并.tif')
XYZ_SRC = ('crs=EPSG:3857&format&interpretation=terrariumterrain&type=xyz'
           '&url=https://terrarium.nextgis.ru/{z}/{x}/{y}.png&zmax=18&zmin=0')
LON, LAT = 114.157112, 37.971395
R3KM = 3000.0

results = []
dem_layer = None
near_layer = None
r2k_layer = None


def check(name, ok, detail=''):
    results.append((name, ok, detail))
    print('%s %s %s' % ('PASS' if ok else 'FAIL', name, detail))


def close_layers():
    """释放缓存目录里的图层句柄（Windows 上句柄不释放会删不掉文件）"""
    global dem_layer, near_layer, r2k_layer
    dem_layer = None
    near_layer = None
    r2k_layer = None
    gc.collect()


def clear_cache():
    close_layers()
    if os.path.isdir(CACHE):
        shutil.rmtree(CACHE, ignore_errors=True)
    if os.path.isdir(CACHE):
        try:
            os.rename(CACHE, CACHE + '_old_%d' % int(time.time()))
        except OSError:
            pass


def clear_dem_only():
    close_layers()
    dem_dir = os.path.join(CACHE, 'dem')
    shutil.rmtree(dem_dir, ignore_errors=True)
    if os.path.isdir(dem_dir):
        try:
            os.rename(dem_dir, dem_dir + '_old_%d' % int(time.time()))
        except OSError:
            pass


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vs = load_module(os.path.join(REPO, 'viewshed.py'), 'viewshed')

app = QgsApplication([], True)
app.initQgis()

# ---------------------------------------------------------------- A 单元
print('=== A. 单元测试')
info = online_dem.parse_xyz_source(XYZ_SRC)
check('识别 terrarium 源',
      info is not None and info['url'].startswith('https://terrarium.nextgis.ru'),
      str(info))
check('拒绝非 terrarium XYZ', online_dem.parse_xyz_source(
    'type=xyz&url=https://example.com/{z}/{x}/{y}.png') is None)
check('拒绝 WMS', online_dem.parse_xyz_source(
    'type=wms&url=https://example.com/wms') is None)
check('拒绝本地路径', online_dem.parse_xyz_source('/data/dem.tif') is None)

n0 = online_dem.tile_range(LON, LAT, R3KM, 13)
check('z13 3km 瓦片范围', n0 == (6692, 6694, 3159, 3161), str(n0))
check('z13 瓦片数=9', (n0[1] - n0[0] + 1) * (n0[3] - n0[2] + 1) == 9)
check('视距 3000 -> z12', online_dem.choose_zoom(LON, LAT, R3KM) == 12,
      'zoom=%d' % online_dem.choose_zoom(LON, LAT, R3KM))
check('视距 1200 -> z13', online_dem.choose_zoom(LON, LAT, 1200) == 13)
check('视距 300 -> z14', online_dem.choose_zoom(LON, LAT, 300) == 14)
check('视距 30000 -> 更粗', online_dem.choose_zoom(LON, LAT, 30000) <= 12)
check('输出分辨率 30m/35m 自适应',
      online_dem.output_px_m(1200) == 30.0 and online_dem.output_px_m(3000) == 35.0,
      '%.0f/%.0f' % (online_dem.output_px_m(1200), online_dem.output_px_m(3000)))

xt, yt = online_dem.lonlat_to_tile(0.0, 0.0, 0)
check('(0,0) z0 -> (0.5,0.5)', abs(xt - 0.5) < 1e-9 and abs(yt - 0.5) < 1e-9)

# Terrarium 解码已知答案：R=128,G=1,B=128 -> 128*256+1+0.5-32768 = 1.5
os.makedirs(CACHE, exist_ok=True)
test_png = os.path.join(CACHE, '_synthetic_rgb.png')
mem = gdal.GetDriverByName('MEM').Create('', 8, 8, 3, gdal.GDT_Byte)
for band_i, val in enumerate((128, 1, 128), start=1):
    mem.GetRasterBand(band_i).WriteArray(np.full((8, 8), val, np.uint8))
gdal.GetDriverByName('PNG').CreateCopy(test_png, mem)
decoded = online_dem.decode_terrarium_png(test_png)
check('Terrarium 解码公式', abs(float(decoded[0, 0]) - 1.5) < 1e-6,
      'got %.4f expect 1.5' % float(decoded[0, 0]))

# ---------------------------------------------------------------- B 冷启动
print('=== B. 冷启动（真清空缓存）')
clear_cache()
t = time.time()
meta = online_dem.build_dem_for_point(XYZ_SRC, LON, LAT, R3KM, cache_dir=CACHE)
build_cold = time.time() - t
check('冷启动生成 DEM', os.path.isfile(meta['path']), meta['path'])
check('冷启动下载瓦片', meta['downloaded'] > 0 and not meta['reused'],
      'tiles=%d dl=%d zoom=%d %.2fs'
      % (meta['tiles'], meta['downloaded'], meta['zoom'], build_cold))

dem_layer = QgsRasterLayer(meta['path'], '在线高程 DEM', 'gdal')
check('DEM 图层有效', dem_layer.isValid(), dem_layer.crs().authid())

t = time.time()
res_online = vs.compute_viewshed(dem_layer, QgsPointXY(LON, LAT), R3KM,
                                 1.7, 0.0, True, 0.02)
compute_online = time.time() - t
check('在线 DEM 视域计算成功', res_online is not None, '%.2fs' % compute_online)
vis_o = 100.0 * res_online['visible_count'] / max(
    1, res_online['visible_count'] + res_online['blocked_count'])
print('   在线: 可见 %.1f%%  地面 %.1fm  %dx%d  %.2fs'
      % (vis_o, res_online['ground_z'], res_online['cols'],
         res_online['rows'], compute_online))

t = time.time()
meta_hit = online_dem.build_dem_for_point(XYZ_SRC, LON, LAT, R3KM, cache_dir=CACHE)
build_hot = time.time() - t
check('同点第二次命中 DEM 缓存', meta_hit['reused'] and build_hot < 0.2,
      '%.3fs' % build_hot)

# DEM 范围按瓦片对齐 -> 同瓦片范围内的相邻点也能共享，且范围必须盖住视距圆
meta_shift = online_dem.build_dem_for_point(XYZ_SRC, LON + 0.0005, LAT, R3KM,
                                            cache_dir=CACHE)
check('相邻点共享 DEM 缓存', meta_shift['reused'], meta_shift['path'])
near_layer = QgsRasterLayer(meta_shift['path'], 'near', 'gdal')
ext = near_layer.extent()
m_lon = 111320.0 * math.cos(math.radians(LAT))
d_lon = R3KM / m_lon
d_lat = R3KM / 111132.95
check('DEM 范围覆盖视距圆',
      ext.xMinimum() <= LON + 0.0005 - d_lon and ext.xMaximum() >= LON + 0.0005 + d_lon
      and ext.yMinimum() <= LAT - d_lat and ext.yMaximum() >= LAT + d_lat,
      '%.4f..%.4f  %.4f..%.4f' % (ext.xMinimum(), ext.xMaximum(),
                                  ext.yMinimum(), ext.yMaximum()))

meta_r2k = online_dem.build_dem_for_point(XYZ_SRC, LON, LAT, 2000, cache_dir=CACHE)
r2k_layer = QgsRasterLayer(meta_r2k['path'], 'r2k', 'gdal')
res_r2k = vs.compute_viewshed(r2k_layer, QgsPointXY(LON, LAT), 2000,
                              1.7, 0.0, True, 0.02)
check('不同半径用同一瓦片 DEM 计算正常',
      res_r2k is not None
      and abs(res_r2k['ground_z'] - res_online['ground_z']) < 2.0,
      'ground=%.1f' % (res_r2k['ground_z'] if res_r2k else -1))

# ---------------------------------------------------------------- D 只清 DEM
print('=== D. 只清 DEM（瓦片保留）')
clear_dem_only()
t = time.time()
meta_rebuild = online_dem.build_dem_for_point(XYZ_SRC, LON, LAT, R3KM, cache_dir=CACHE)
rebuild = time.time() - t
check('瓦片缓存复用、不重新下载',
      meta_rebuild['downloaded'] == 0 and not meta_rebuild['reused'],
      'downloaded=%d %.2fs' % (meta_rebuild['downloaded'], rebuild))

# ---------------------------------------------------------------- E 断网
print('=== E. 断网/坏地址')
offline_dir = r'D:\Temp\pb_test\offline_cache'
shutil.rmtree(offline_dir, ignore_errors=True)
t = time.time()
offline_ok = False
offline_msg = ''
try:
    online_dem.build_dem_for_point(XYZ_SRC, LON, LAT, 600,
                                   cache_dir=offline_dir,
                                   url_override='https://127.0.0.1:9/{z}/{x}/{y}.png')
except online_dem.OnlineDemError as exc:
    offline_ok = True
    offline_msg = str(exc).replace('\n', ' ')
except Exception as exc:  # noqa: BLE001
    offline_msg = 'wrong error type: %r' % exc
offline_time = time.time() - t
check('断网快速报错（<15s）', offline_ok and offline_time < 15.0,
      '%.2fs %s' % (offline_time, offline_msg[:110]))

try:
    online_dem.build_dem_for_point('type=xyz&url=https://example.com/{z}/{x}/{y}.png',
                                   LON, LAT, 600, cache_dir=offline_dir)
    rejected = False
except online_dem.OnlineDemError:
    rejected = True
check('非 terrarium 源拒绝', rejected)

# ---------------------------------------------------------------- F 交叉验证
print('=== F. 与本地 合并.tif 交叉验证')
local = QgsRasterLayer(LOCAL_TIF, 'local')
t = time.time()
res_local = vs.compute_viewshed(local, QgsPointXY(LON, LAT), R3KM,
                                1.7, 0.0, True, 0.02)
compute_local = time.time() - t
vis_l = 100.0 * res_local['visible_count'] / max(
    1, res_local['visible_count'] + res_local['blocked_count'])
print('   本地: 可见 %.1f%%  地面 %.1fm  %dx%d  %.2fs'
      % (vis_l, res_local['ground_z'], res_local['cols'],
         res_local['rows'], compute_local))


def mask_on_common_grid(res, half_m=3090.0, px_m=15.0):
    m_lon = 111320.0 * math.cos(math.radians(LAT))
    dlon, dlat = px_m / m_lon, px_m / 111132.95
    n = int(math.ceil(2 * half_m / px_m))
    lon_c = LON - n / 2.0 * dlon + np.arange(n) * dlon + dlon / 2.0
    lat_c = LAT + n / 2.0 * dlat - np.arange(n) * dlat - dlat / 2.0
    gx, gy = np.meshgrid(lon_c, lat_c)
    ext = res['extent']
    px_x, px_y = res['px']
    col = (gx - ext.xMinimum()) / px_x
    row = (ext.yMaximum() - gy) / px_y
    ci = np.clip(np.floor(col).astype(int), 0, res['cols'] - 1)
    ri = np.clip(np.floor(row).astype(int), 0, res['rows'] - 1)
    grid = np.array([[1 if v == 1 else 0 for v in r] for r in res['grid']],
                    dtype=np.uint8)
    return grid[ri, ci]


gl = mask_on_common_grid(res_local)
go = mask_on_common_grid(res_online)
inter = np.logical_and(gl, go).sum()
union = np.logical_or(gl, go).sum()
iou = inter / max(1, union)
d_ground = abs(res_online['ground_z'] - res_local['ground_z'])
print('   在线 vs 本地: IoU=%.2f  可见比例 %.1f%% vs %.1f%%  地面差 %.1fm'
      % (iou, vis_o, vis_l, d_ground))
check('可见比例大致一致（±5pp）', abs(vis_o - vis_l) <= 5.0,
      '%.1f vs %.1f' % (vis_o, vis_l))
check('形状 IoU ≥ 0.7', iou >= 0.7, 'IoU=%.2f' % iou)
check('地面高程一致（±10m）', d_ground <= 10.0, '%.1fm' % d_ground)

# ---------------------------------------------------------------- G 端到端
print('=== G. 视域工具端到端（无头 canvas）')
timings = {}
orig_compute = vt.vs_mod.compute_viewshed
orig_draw = vt.ViewshedTool._draw_result


def timed_compute(*args, **kwargs):
    t0 = time.time()
    out = orig_compute(*args, **kwargs)
    timings['compute'] = time.time() - t0
    return out


def timed_draw(self, *args, **kwargs):
    t0 = time.time()
    out = orig_draw(self, *args, **kwargs)
    timings['draw'] = time.time() - t0
    return out


vt.vs_mod.compute_viewshed = timed_compute
vt.ViewshedTool._draw_result = timed_draw

canvas = QgsMapCanvas()
canvas.setDestinationCrs(QgsCoordinateReferenceSystem('EPSG:3857'))
canvas.setExtent(QgsRectangle(12700000, 4580000, 12760000, 4610000))
xyz_layer = QgsRasterLayer(XYZ_SRC, 'NextGIS 真实高程', 'wms')
print('   XYZ 图层有效:', xyz_layer.isValid(),
      '| terrarium:', online_dem.layer_is_terrarium(xyz_layer))
project = QgsProject.instance()
project.addMapLayer(xyz_layer, False)

tr = QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'),
                            QgsCoordinateReferenceSystem('EPSG:3857'),
                            QgsProject.instance())
center_3857 = tr.transform(QgsPointXY(LON, LAT))

clear_cache()
tool = vt.ViewshedTool(canvas, None, None)
tool.settings.dem_layer_id = xyz_layer.id()
tool.center = center_3857
tool.radius_m = R3KM
t = time.time()
tool.run_analysis()
first_run = time.time() - t
check('端到端首次（含下载）< 5s', first_run < 5.0,
      '%.2fs  计算 %.2fs 绘制 %.2fs'
      % (first_run, timings.get('compute', -1), timings.get('draw', -1)))
check('结果已锁存', tool.locked)

t = time.time()
tool.run_analysis()
second_same = time.time() - t
check('同点第二次（同实例）< 1s', second_same < 1.0, '%.2fs' % second_same)

# 模拟重启 QGIS：新工具实例，仅靠磁盘缓存
tool2 = vt.ViewshedTool(canvas, None, None)
tool2.settings.dem_layer_id = xyz_layer.id()
tool2.center = center_3857
tool2.radius_m = R3KM
t = time.time()
tool2.run_analysis()
second_new = time.time() - t
check('同点第二次（新实例/磁盘缓存）< 1s', second_new < 1.0,
      '%.2fs  绘制 %.2fs' % (second_new, timings.get('draw', -1)))

# 附近新点（同瓦片范围）：瓦片缓存复用，重新计算
tool2.reset_all()
tool2.center = tr.transform(QgsPointXY(LON + 0.004, LAT + 0.001))
tool2.radius_m = R3KM
t = time.time()
tool2.run_analysis()
nearby_run = time.time() - t
print('   附近新点（瓦片缓存复用）: %.2fs  计算 %.2fs 绘制 %.2fs'
      % (nearby_run, timings.get('compute', -1), timings.get('draw', -1)))
check('附近新点可计算', tool2.locked)

# 本地 DEM 回归：路径不变、结果正常
print('=== H. 本地 DEM 回归（工具路径）')
local_layer = QgsRasterLayer(LOCAL_TIF, '合并')
project.addMapLayer(local_layer, False)
tool3 = vt.ViewshedTool(canvas, None, None)
tool3.settings.dem_layer_id = local_layer.id()
tool3.center = center_3857
tool3.radius_m = R3KM
t = time.time()
tool3.run_analysis()
local_run = time.time() - t
check('本地 DEM 工具路径正常', tool3.locked,
      '%.2fs  计算 %.2fs' % (local_run, timings.get('compute', -1)))
t = time.time()
tool3.run_analysis()
local_second = time.time() - t
check('本地 DEM 二次计算（结果缓存）< 1s', local_second < 1.0, '%.2fs' % local_second)

vt.vs_mod.compute_viewshed = orig_compute
vt.ViewshedTool._draw_result = orig_draw

print()
failed = [r for r in results if not r[1]]
print('=== 汇总: %d/%d PASS' % (len(results) - len(failed), len(results)))
for name, ok, detail in results:
    if not ok:
        print('  FAILED:', name, detail)

app.exitQgis()
sys.exit(1 if failed else 0)
