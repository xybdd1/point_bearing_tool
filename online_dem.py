# -*- coding: utf-8 -*-
"""
online_dem.py - 在线高程源（Terrarium XYZ 瓦片）支持

问题背景
--------
视域算法要读几万个像元。QGIS 的 XYZ 图层每次取像元都要联网，读几万像元
就是几万次请求，QGIS 会直接卡死。所以 v1.3.1 直接拒绝在线图层。

思路
----
先把视距圆覆盖的少量瓦片下载到本地缓存（每块 256×256 PNG，几十 KB），
按 Terrarium 公式解码成高程米数，拼成本地 GeoTIFF（EPSG:4326，约 30m），
再交给现有 viewshed 算法 —— 算法只看本地栅格，不再联网。

Terrarium 解码公式（RGB 三通道拼高程，不是灰度图）：
    elevation_m = R * 256 + G + B / 256 - 32768

缓存布局（默认 D:\\Temp\\point_bearing_cache，可清空）：
    z{zoom}\\{x}\\{y}.png                     原始瓦片，已有就不重复下载
    dem\\z{z}_x{x0}-{x1}_y{y0}-{y1}_{px}m.tif  拼好的 DEM，命中直接复用

本模块不依赖 qgis 包（只依赖 numpy + GDAL），方便无头测试。
"""

import http.client
import math
import os
import ssl
import socket
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

# 用户约定：缓存等大文件放 D:\Temp，不写 C 盘
DEFAULT_CACHE_DIR = r'D:\Temp\point_bearing_cache'
CACHE_DIR_ENV = 'POINT_BEARING_CACHE_DIR'

HALF_WORLD = 20037508.342789244
WORLD_SIZE = 2.0 * HALF_WORLD
M_PER_DEG_LAT = 111132.95
M_PER_DEG_LON_EQ = 111320.0
MAX_LAT_MERCATOR = 85.05112878

OUT_PX_M = 30.0          # 输出 DEM 分辨率（米，与本地 30m 参考一致）
OUT_PX_M_FAR = 35.0      # 大视距时略粗一点，控制纯 Python 射线扫描耗时
MAX_LAT_MERCATOR = 85.05112878
PAD_M = 90.0             # 视距圆外扩（约 3 个输出像元，视域算法自己还要外扩 3 像元）
MAX_TILES = 12           # 单次最多下载瓦片数，超过就降 zoom
TILE_TIMEOUT = 10.0      # 单块瓦片超时（秒）
TILE_ATTEMPTS = 2        # 直连重试次数
MAX_WORKERS = 10         # 并发下载连接数
UA = 'point_bearing_tool/1.4 (QGIS viewshed online DEM)'


class OnlineDemError(Exception):
    """在线高程的可展示错误（中文，给 messageBar/对话框用）"""


# ---------------------------------------------------------------------------
# 识别与解析
# ---------------------------------------------------------------------------
def parse_xyz_source(source):
    """解析 QGIS XYZ 图层源字符串，返回 dict 或 None

    典型源字符串：
        crs=EPSG:3857&format&interpretation=terrariumterrain&type=xyz
        &url=https://terrarium.nextgis.ru/{z}/{x}/{y}.png&zmax=18&zmin=0
    """
    if not source:
        return None
    low = source.lower()
    if 'type=xyz' not in low:
        return None

    parts = {}
    for token in source.split('&'):
        if '=' in token:
            key, value = token.split('=', 1)
            parts[key.strip().lower()] = urllib.parse.unquote(value.strip())

    url = parts.get('url', '')
    if not url:
        return None

    interpretation = parts.get('interpretation', '').lower()
    low_url = url.lower()
    is_terrarium = ('terrarium.nextgis.ru' in low_url
                    or 'terrarium' in interpretation)
    if not is_terrarium:
        return None

    if '{z}' not in url or '{x}' not in url or '{y}' not in url:
        return None

    try:
        zmin = int(parts.get('zmin', '0'))
    except ValueError:
        zmin = 0
    try:
        zmax = int(parts.get('zmax', '18'))
    except ValueError:
        zmax = 18
    return {'url': url, 'zmin': zmin, 'zmax': zmax}


def layer_is_terrarium(layer):
    """图层是不是受支持的 Terrarium XYZ 在线高程源"""
    try:
        provider = layer.dataProvider()
        source = provider.dataSourceUri() if provider is not None else ''
    except (AttributeError, RuntimeError, TypeError):
        return False
    return parse_xyz_source(source) is not None


# ---------------------------------------------------------------------------
# 瓦片数学
# ---------------------------------------------------------------------------
def tile_size_m(zoom):
    """该 zoom 下每块瓦片的 Web 墨卡托宽度（米）"""
    return WORLD_SIZE / float(1 << zoom)


def lonlat_to_tile(lon, lat, zoom):
    """经纬度 -> 该 zoom 下浮点瓦片坐标"""
    n = float(1 << zoom)
    lat = max(-MAX_LAT_MERCATOR, min(MAX_LAT_MERCATOR, lat))
    xt = (lon + 180.0) / 360.0 * n
    yt = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return xt, yt


def tile_range(lon, lat, radius_m, zoom, pad_m=PAD_M):
    """视距圆（外扩 pad_m）覆盖的瓦片编号范围 (x0, x1, y0, y1)，含边界"""
    m_lon = M_PER_DEG_LON_EQ * max(0.01, math.cos(math.radians(lat)))
    dlon = (radius_m + pad_m) / m_lon
    dlat = (radius_m + pad_m) / M_PER_DEG_LAT
    n = 1 << zoom

    xt0, yt0 = lonlat_to_tile(lon - dlon, lat + dlat, zoom)
    xt1, yt1 = lonlat_to_tile(lon + dlon, lat - dlat, zoom)
    x0 = max(0, int(math.floor(xt0)))
    x1 = min(n - 1, int(math.floor(xt1)))
    y0 = max(0, int(math.floor(yt0)))
    y1 = min(n - 1, int(math.floor(yt1)))
    return x0, x1, y0, y1


def choose_zoom(lon, lat, radius_m, zmin=0, zmax=18):
    """按视距选 zoom：近处细、远处粗，且瓦片数不超过 MAX_TILES

    经验值（实测：到 terrarium.nextgis.ru 单块 20–120KB、速度波动大）：
        视距 ≤ 400m   -> z14（原生 7.5m，1–4 块）
        视距 ≤ 1200m  -> z13（原生 15m，2–9 块）
        视距 > 1200m  -> z12（原生 30m，4–9 块，正好等于输出分辨率 30m）
    大视距用粗 zoom 是为了把下载量压下来：3km 视距 z14 要 25 块约 2.6MB，
    而 z12 只要 4 块约 120KB，数据精度对 30m 输出没有实际损失。
    """
    zmin = max(0, min(18, int(zmin or 0)))
    zmax = max(zmin, min(18, int(zmax if zmax is not None else 18)))

    if radius_m <= 400.0:
        zoom = 14
    elif radius_m <= 1200.0:
        zoom = 13
    else:
        zoom = 12
    zoom = max(zmin, min(zmax, zoom))

    while zoom > zmin:
        x0, x1, y0, y1 = tile_range(lon, lat, radius_m, zoom)
        if (x1 - x0 + 1) * (y1 - y0 + 1) <= MAX_TILES:
            break
        zoom -= 1
    return zoom


# ---------------------------------------------------------------------------
# 缓存目录
# ---------------------------------------------------------------------------
def resolve_cache_dir(explicit=None):
    """挑一个可写的缓存目录：参数 > 环境变量 > D:\\Temp > 插件目录 > 系统临时目录"""
    candidates = [explicit, os.environ.get(CACHE_DIR_ENV),
                  DEFAULT_CACHE_DIR,
                  os.path.join(PLUGIN_DIR, 'cache'),
                  os.path.join(tempfile.gettempdir(), 'point_bearing_cache')]
    last_error = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, '.write_probe')
            with open(probe, 'w') as f:
                f.write('ok')
            os.remove(probe)
            return candidate
        except OSError as exc:
            last_error = exc
            continue
    raise OnlineDemError('找不到可写的缓存目录（可设置环境变量 %s）\n%s'
                         % (CACHE_DIR_ENV, last_error))


def tile_cache_path(cache_dir, zoom, x, y):
    return os.path.join(cache_dir, 'z%d' % zoom, str(x), '%d.png' % y)


def output_px_m(radius_m):
    """输出分辨率：近处 30m（与本地 DEM 一致），远处 35m 控制算法耗时

    实测 3km 视域：35m 输出与 30m 本地 DEM 的可见形状 IoU≈0.78、可见比例
    只差 +1.7pp，肉眼基本看不出差别，但纯 Python 射线扫描从 ~1.6s 降到 ~1.2s。
    """
    return OUT_PX_M_FAR if radius_m > 1500.0 else OUT_PX_M


def dem_cache_path(cache_dir, zoom, x0, x1, y0, y1, px_m=OUT_PX_M):
    """DEM 缓存按瓦片范围+分辨率命名

    输出栅格范围对齐瓦片边界（与观察点/半径无关），所以同瓦片范围内的
    不同观察点可以直接共享同一个 DEM 文件。
    """
    return os.path.join(cache_dir, 'dem', 'z%d_x%d-%d_y%d-%d_%dm.tif'
                        % (zoom, x0, x1, y0, y1, int(px_m)))


# ---------------------------------------------------------------------------
# 下载（并行 keep-alive，失败回退 urllib 走系统代理）
# ---------------------------------------------------------------------------
class _TileMissing(Exception):
    """瓦片返回 404（该处无数据，用 nodata 填）"""


# 复用 SSL 上下文，避免每块瓦片都重新加载 CA 证书
_SSL_CONTEXT = None


def _ssl_context():
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _fetch_tile(url, state, timeout):
    """用 keep-alive 连接取一块瓦片；state 保存跨调用复用的连接"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https'):
        raise OnlineDemError('不支持的瓦片地址：%s' % url)

    conn = state.get('conn')
    if conn is None or state.get('host') != parsed.hostname:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if parsed.scheme == 'https':
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                               timeout=timeout,
                                               context=_ssl_context())
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80,
                                              timeout=timeout)
        state['conn'] = conn
        state['host'] = parsed.hostname

    request_path = parsed.path + ('?' + parsed.query if parsed.query else '')
    conn.request('GET', request_path,
                 headers={'User-Agent': UA, 'Connection': 'keep-alive'})
    response = conn.getresponse()
    body = response.read()
    if response.status == 404:
        raise _TileMissing(url)
    if response.status != 200:
        raise OSError('HTTP %d' % response.status)
    return body


def _urllib_get(url, timeout):
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ('http', 'https'):
        raise OnlineDemError('不支持的瓦片地址：%s' % url)
    request = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - 仅允许 http/https
        return response.read()


def _close_state(state):
    conn = state.get('conn')
    state['conn'] = None
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, '.tmp_%s' % uuid.uuid4().hex)
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def _download_worker(items, timeout, counter):
    state = {'conn': None, 'host': None}
    try:
        for url, path in items:
            success = False
            last_error = None
            for attempt in range(TILE_ATTEMPTS):
                try:
                    body = _fetch_tile(url, state, timeout)
                except _TileMissing:
                    counter['missing'] += 1
                    success = True
                    break
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    last_error = exc
                    _close_state(state)
                    time.sleep(0.4 * (attempt + 1))
                    continue
                _atomic_write(path, body)
                counter['done'] += 1
                success = True
                break

            if not success:
                # 直连全部失败 -> 回退 urllib（会走系统代理），超时收紧
                try:
                    body = _urllib_get(url, min(timeout, 6.0))
                except Exception as exc:  # noqa: BLE001 - 统一转业务错误
                    raise OnlineDemError(
                        '下载在线高程瓦片失败：%s\n原因：%s\n请检查网络/代理后重试。'
                        % (url, last_error or exc))
                _atomic_write(path, body)
                counter['done'] += 1
    finally:
        _close_state(state)


def _preflight(host, port, timeout=5.0):
    """连一下服务器，断网时快速失败；直连失败但配了代理就检查代理"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return
    except OSError as exc:
        direct_error = exc

    proxies = urllib.request.getproxies()
    proxy = proxies.get('https') or proxies.get('http')
    if proxy:
        parsed = urllib.parse.urlsplit(proxy if '://' in proxy else 'http://' + proxy)
        try:
            with socket.create_connection((parsed.hostname, parsed.port or 8080),
                                          timeout=timeout):
                return
        except OSError:
            pass
    raise OnlineDemError(
        '无法连接在线高程服务 %s。\n原因：%s\n请检查网络或代理设置后重试。'
        % (host, direct_error))


def _download_tiles(jobs, progress=None, stall_timeout=10.0):
    """并行下载 jobs=[(url, path)...]，返回已下载数

    stall_timeout：连续这么多秒没有一块瓦片完成（首次或中途）就判定服务不可达，
    立刻抛错，避免断网时干等。
    """
    if not jobs:
        return 0
    total = len(jobs)
    workers = max(1, min(MAX_WORKERS, total))
    chunks = [jobs[i::workers] for i in range(workers)]
    counter = {'done': 0, 'missing': 0}
    errors = []

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(_download_worker, chunk, TILE_TIMEOUT, counter)
                   for chunk in chunks]
        pending = set(futures)
        started = time.time()
        last_done = None
        while pending:
            done, pending = wait(pending, timeout=0.2,
                                 return_when=FIRST_COMPLETED)
            for future in done:
                error = future.exception()
                if error is not None:
                    errors.append(error)
            if progress is not None:
                progress('download', counter['done'] + counter['missing'], total)

            now = time.time()
            if done:
                last_done = now
            reference = last_done if last_done is not None else started
            if now - reference > stall_timeout:
                errors.append(OnlineDemError(
                    '在线高程服务长时间没有响应（%.0f 秒无瓦片返回）。\n'
                    '请检查网络或代理设置后重试。' % stall_timeout))
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    if errors:
        raise errors[0]
    return counter['done']


# ---------------------------------------------------------------------------
# 解码 / 拼接 / 重采样
# ---------------------------------------------------------------------------
def decode_terrarium_png(path):
    """读取一块 Terrarium PNG，返回 float64 高程数组（像素）"""
    dataset = gdal.Open(path)
    if dataset is None:
        raise OnlineDemError('瓦片无法读取：%s' % path)
    array = dataset.ReadAsArray()
    dataset = None
    if array.ndim == 2:
        array = np.stack([array, array, array])
    if array.shape[0] < 3:
        raise OnlineDemError('瓦片不是 RGB 格式：%s' % path)
    red = array[0].astype(np.float64)
    green = array[1].astype(np.float64)
    blue = array[2].astype(np.float64)
    return red * 256.0 + green + blue / 256.0 - 32768.0


def _mosaic_from_tiles(cache_dir, zoom, x0, x1, y0, y1):
    """把瓦片拼成 3857 网格，返回 (array, geotransform, 缺瓦片数)"""
    tile_px = 256
    width = (x1 - x0 + 1) * tile_px
    height = (y1 - y0 + 1) * tile_px
    mosaic = np.full((height, width), np.nan, dtype=np.float32)
    missing = 0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            path = tile_cache_path(cache_dir, zoom, x, y)
            if not (os.path.exists(path) and os.path.getsize(path) > 0):
                missing += 1
                continue
            try:
                decoded = decode_terrarium_png(path)
            except (OnlineDemError, RuntimeError):
                missing += 1
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
            ox = (x - x0) * tile_px
            oy = (y - y0) * tile_px
            mosaic[oy:oy + tile_px, ox:ox + tile_px] = decoded

    tile_m = tile_size_m(zoom)
    geotransform = (x0 * tile_m - HALF_WORLD, tile_m / tile_px, 0.0,
                    HALF_WORLD - y0 * tile_m, 0.0, -tile_m / tile_px)
    return mosaic, geotransform, missing


def tile_bounds_4326(zoom, x0, x1, y0, y1):
    """瓦片范围在 EPSG:4326 下的包围盒 (lon_min, lat_min, lon_max, lat_max)"""
    tile_m = tile_size_m(zoom)
    x_min = x0 * tile_m - HALF_WORLD
    x_max = (x1 + 1) * tile_m - HALF_WORLD
    y_max = HALF_WORLD - y0 * tile_m
    y_min = HALF_WORLD - (y1 + 1) * tile_m

    def lon_of(x):
        return x * 180.0 / HALF_WORLD

    def lat_of(y):
        return math.degrees(math.atan(math.sinh(y * math.pi / HALF_WORLD)))

    return lon_of(x_min), lat_of(y_min), lon_of(x_max), lat_of(y_max)


def resample_to_4326(mosaic, geotransform, bbox, lat_ref, px_m=OUT_PX_M):
    """把 3857 栅格重采样成 EPSG:4326 网格（按参考纬度的真实米数）

    bbox = (lon_min, lat_min, lon_max, lat_max)，输出栅格对齐到它；
    返回 (array, geotransform4326)。用 numpy 反投影，避免 gdal.Warp 的进程开销。
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    m_lon = M_PER_DEG_LON_EQ * max(0.01, math.cos(math.radians(lat_ref)))
    dlon = px_m / m_lon
    dlat = px_m / M_PER_DEG_LAT
    n_cols = max(2, int(math.ceil((lon_max - lon_min) / dlon)))
    n_rows = max(2, int(math.ceil((lat_max - lat_min) / dlat)))

    lon_centers = lon_min + (np.arange(n_cols) + 0.5) * dlon
    lat_centers = lat_max - (np.arange(n_rows) + 0.5) * dlat
    grid_lon, grid_lat = np.meshgrid(lon_centers, lat_centers)

    x3857 = grid_lon * (HALF_WORLD / 180.0)
    y3857 = np.log(np.tan(math.pi / 4.0 + np.radians(grid_lat) / 2.0)) * (HALF_WORLD / math.pi)

    col = (x3857 - geotransform[0]) / geotransform[1] - 0.5
    row = (y3857 - geotransform[3]) / geotransform[5] - 0.5
    c0 = np.clip(np.floor(col).astype(np.int64), 0, mosaic.shape[1] - 2)
    r0 = np.clip(np.floor(row).astype(np.int64), 0, mosaic.shape[0] - 2)
    fx = col - c0
    fy = row - r0

    v00 = mosaic[r0, c0]
    v10 = mosaic[r0, c0 + 1]
    v01 = mosaic[r0 + 1, c0]
    v11 = mosaic[r0 + 1, c0 + 1]

    top = v00 * (1.0 - fx) + v10 * fx
    bottom = v01 * (1.0 - fx) + v11 * fx
    result = top * (1.0 - fy) + bottom * fy

    geotransform_out = (lon_min, dlon, 0.0, lat_max, 0.0, -dlat)
    return result.astype(np.float32), geotransform_out


def _write_tif(array, geotransform, path):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, '.tmp_%s.tif' % uuid.uuid4().hex)
    dataset = gdal.GetDriverByName('GTiff').Create(
        tmp, array.shape[1], array.shape[0], 1, gdal.GDT_Float32,
        options=['COMPRESS=DEFLATE', 'TILED=YES'])
    if dataset is None:
        raise OnlineDemError('无法创建 DEM 文件：%s' % path)
    try:
        spatial_ref = osr.SpatialReference()
        spatial_ref.ImportFromEPSG(4326)
        dataset.SetProjection(spatial_ref.ExportToWkt())
        dataset.SetGeoTransform(geotransform)
        band = dataset.GetRasterBand(1)
        data = np.where(np.isfinite(array), array, -32768.0).astype(np.float32)
        band.WriteArray(data)
        band.SetNoDataValue(-32768.0)
        band.FlushCache()
    finally:
        dataset = None
    os.replace(tmp, path)


def _tif_ok(path):
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return False
    try:
        dataset = gdal.Open(path)
        if dataset is None:
            return False
        ok = dataset.RasterCount >= 1 and dataset.RasterXSize > 1
        dataset = None
        return ok
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# 对外主流程
# ---------------------------------------------------------------------------
def build_dem_for_point(source, lon, lat, radius_m, cache_dir=None,
                        progress=None, url_override=None):
    """为观察点生成/复用本地 DEM GeoTIFF

    参数：
        source        QGIS XYZ 图层源字符串（或 Nature 的 url 模板）
        lon, lat      观察点经纬度（EPSG:4326）
        radius_m      视距（米）
        cache_dir     缓存目录，None = 自动选
        progress      progress(phase, done, total) 回调，phase='download'/'build'
        url_override  测试用：替换瓦片 URL 模板

    返回 dict：path/zoom/tiles/cached/downloaded/missing/elapsed/reused/px_m
    失败抛 OnlineDemError（中文，可直接展示）
    """
    started = time.time()
    info = parse_xyz_source(source)
    if info is None:
        raise OnlineDemError('不是受支持的在线高程源（需要 XYZ + Terrarium 图层）')

    url_template = url_override or info['url']
    lon = float(lon)
    lat = float(lat)
    radius_m = float(radius_m)
    if not (-MAX_LAT_MERCATOR <= lat <= MAX_LAT_MERCATOR):
        raise OnlineDemError('观察点纬度超出 Web 墨卡托范围（±85.05°）')
    if radius_m <= 0:
        raise OnlineDemError('视距必须大于 0 米')

    cache = resolve_cache_dir(cache_dir)
    zoom = choose_zoom(lon, lat, radius_m, info['zmin'], info['zmax'])
    px_m = output_px_m(radius_m)
    x0, x1, y0, y1 = tile_range(lon, lat, radius_m, zoom)
    if x1 < x0 or y1 < y0:
        raise OnlineDemError('观察点附近没有可用的在线高程瓦片')

    dem_path = dem_cache_path(cache, zoom, x0, x1, y0, y1, px_m)
    if _tif_ok(dem_path):
        return {'path': dem_path, 'zoom': zoom, 'px_m': px_m,
                'tiles': 0, 'cached': 0, 'downloaded': 0, 'missing': 0,
                'elapsed': time.time() - started, 'reused': True,
                'cache_dir': cache}

    tiles_total = 0
    jobs = []
    cached = 0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            tiles_total += 1
            path = tile_cache_path(cache, zoom, x, y)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                cached += 1
            else:
                url = url_template.replace('{z}', str(zoom)) \
                                  .replace('{x}', str(x)) \
                                  .replace('{y}', str(y)) \
                                  .replace('{-y}', str((1 << zoom) - 1 - y))
                jobs.append((url, path))

    downloaded = 0
    if jobs:
        parsed = urllib.parse.urlsplit(jobs[0][0])
        _preflight(parsed.hostname, parsed.port or 443)
        if progress is not None:
            progress('download', cached, tiles_total)
        downloaded = _download_tiles(jobs, progress)

    if progress is not None:
        progress('build', 1, 1)

    mosaic, geotransform, missing = _mosaic_from_tiles(cache, zoom, x0, x1, y0, y1)
    if missing > max(1, tiles_total // 2):
        raise OnlineDemError('在线高程瓦片缺失过多（%d/%d），请检查网络后重试'
                             % (missing, tiles_total))

    array, geotransform_4326 = resample_to_4326(
        mosaic, geotransform, tile_bounds_4326(zoom, x0, x1, y0, y1), lat, px_m)
    _write_tif(array, geotransform_4326, dem_path)

    return {'path': dem_path, 'zoom': zoom, 'px_m': px_m,
            'tiles': tiles_total, 'cached': cached, 'downloaded': downloaded,
            'missing': missing, 'elapsed': time.time() - started,
            'reused': False, 'cache_dir': cache}
