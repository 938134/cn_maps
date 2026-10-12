"""坐标系投影 + 瓦片规划。

合并了原来的 projection.py（WGS84 / GCJ-02 坐标换算）
和 tilemath.py（把「输出瓦片」换算成「要抓哪几张上游瓦片、各画在哪里」）。

纯 Python、零依赖，方便单测。

一个容易踩的点：GCJ-02 偏移只在中国大陆范围内生效，境外必须原样返回。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sources import MapSource

TILE_SIZE = 256

_PI = math.pi

# --- GCJ-02（国测局）椭球参数 ---------------------------------------------
GCJ_A = 6378245.0
GCJ_EE = 0.00669342162296594323


# ------------------------------------------------------------ WGS84 -> GCJ-02
def out_of_china(lng: float, lat: float) -> bool:
    """是否在中国大陆范围之外（境外不做偏移）。"""
    return not (lng > 73.66 and lng < 135.05 and lat > 3.86 and lat < 53.55)


def transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += ((20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0) / 3.0
    ret += ((20.0 * math.sin(y * _PI) + 40.0 * math.sin((y / 3.0) * _PI)) * 2.0) / 3.0
    ret += (
        (160.0 * math.sin((y / 12.0) * _PI) + 320.0 * math.sin((y * _PI) / 30.0)) * 2.0
    ) / 3.0
    return ret


def transform_lng(x: float, y: float) -> float:
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += ((20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0) / 3.0
    ret += ((20.0 * math.sin(x * _PI) + 40.0 * math.sin((x / 3.0) * _PI)) * 2.0) / 3.0
    ret += (
        (150.0 * math.sin((x / 12.0) * _PI) + 300.0 * math.sin((x / 30.0) * _PI)) * 2.0
    ) / 3.0
    return ret


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """WGS84 经纬度 -> GCJ-02 经纬度。"""
    if out_of_china(lng, lat):
        return (lng, lat)
    d_lat = transform_lat(lng - 105.0, lat - 35.0)
    d_lng = transform_lng(lng - 105.0, lat - 35.0)
    rad_lat = (lat / 180.0) * _PI
    magic = math.sin(rad_lat)
    magic = 1 - GCJ_EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / (((GCJ_A * (1 - GCJ_EE)) / (magic * sqrt_magic)) * _PI)
    d_lng = (d_lng * 180.0) / ((GCJ_A / sqrt_magic) * math.cos(rad_lat) * _PI)
    return (lng + d_lng, lat + d_lat)


def datum_lnglat(
    lng: float, lat: float, datum: str, fix: bool = True
) -> tuple[float, float]:
    """把 WGS84 经纬度换算成数据源自己的坐标系。

    ``fix=False``（不纠偏）时原样返回：适用于数据源本身就是 WGS84、或者用户
    的实体坐标本来就是 GCJ-02 的情况。
    """
    if not fix or datum == "wgs84":
        return (lng, lat)
    return wgs84_to_gcj02(lng, lat)


# ---------------------------------------------------------------- 瓦片号换算
def tile_x_to_lon(x: float, z: int) -> float:
    """Web Mercator：瓦片 x（可为小数）对应的经度。"""
    return (x / 2**z) * 360 - 180


def tile_y_to_lat(y: float, z: int) -> float:
    """Web Mercator：瓦片 y（可为小数）对应的纬度。"""
    n = _PI - (2 * _PI * y) / 2**z
    return (180 / _PI) * math.atan(0.5 * (math.exp(n) - math.exp(-n)))


def merc_y(lat: float, z: int) -> float:
    """Web Mercator 的绝对像素 Y（越小越靠北）。"""
    s = math.sin((lat * _PI) / 180)
    return (0.5 - math.log((1 + s) / (1 - s)) / (4 * _PI)) * TILE_SIZE * 2**z


def wrap_x(x: int, limit: int) -> int:
    """把瓦片 x 折回 [0, limit)（跨东经 180° 时用）。"""
    return ((x % limit) + limit) % limit


# ================================================================ 瓦片规划
#
# 以下原 tilemath.py：把「输出瓦片 (z,x,y)」换算成「要抓哪几张上游瓦片、各
# 画在哪里」。输出瓦片是 HA 地图按 WGS84 网格请求的那一张；上游瓦片是数据
# 源自己的网格。两者之间差一个坐标系偏移和可能的级别差。

_PLACEHOLDER = re.compile(r"\{(-?\w+)\}")


@dataclass(frozen=True, slots=True)
class PlannedTile:
    """一张上游瓦片：网格坐标 + 未裁切时在输出瓦片里的像素位置。"""

    x: int
    y: int
    px: float
    py: float


@dataclass(frozen=True, slots=True)
class TilePlan:
    z: int
    x: int
    y: int
    source_level: int
    grid: str
    limit: float
    tile_px_x: float
    tile_px_y: float
    offset_x: int
    offset_y: int
    tiles: tuple[PlannedTile, ...]
    # 只有一张、1:1、且没有偏移 —— 上游字节可以直接透传
    passthrough: bool


# ------------------------------------------------------------------ 地址模板
def tile_url(
    source: MapSource, level: int, tx: int, ty: int, layer: int = 0, key: str = ""
) -> str:
    """按数据源的模板拼出真实的上游瓦片地址。"""
    subs = source.subdomains
    sub = subs[((tx + ty) % len(subs) + len(subs)) % len(subs)]
    data = {
        "s": sub,
        "z": level,
        "x": tx,
        "y": ty,
        "-y": 2**level - 1 - ty,  # 腾讯：上下翻转
        "sx": tx >> 4,  # 腾讯卫星：目录号
        "sy": (2**level - ty) >> 4,
        "tk": key or "",
    }

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(data[name]) if name in data else match.group(0)

    return _PLACEHOLDER.sub(_sub, source.layers[layer])


def source_urls(
    source: MapSource, level: int, tx: int, ty: int, key: str = ""
) -> list[str]:
    """同一张上游瓦片在各图层上的地址（顺序 = 从下往上）。"""
    return [tile_url(source, level, tx, ty, i, key) for i in range(len(source.layers))]


# -------------------------------------------------------------------- 规划
def tile_in_range(tile: PlannedTile, plan: TilePlan) -> bool:
    """这张候选瓦片要不要去抓。"""
    if plan.limit == math.inf:
        return True
    return 0 <= tile.y < plan.limit


def plan_targets(
    source: MapSource, plan: TilePlan, key: str = "", layer: int = 0
) -> list[tuple[str, float, float]]:
    """(上游地址, 画在输出瓦片的 dx, dy) —— dx/dy 可能为负。"""
    out: list[tuple[str, float, float]] = []
    for tile in plan.tiles:
        if not tile_in_range(tile, plan):
            continue
        out.append(
            (
                tile_url(source, plan.source_level, tile.x, tile.y, layer, key),
                tile.px - plan.offset_x,
                tile.py - plan.offset_y,
            )
        )
    return out


def plan_tile(
    source: MapSource,
    z: int,
    x: int,
    y: int,
    *,
    fix_offset: bool = True,
    precise: bool = True,
) -> TilePlan:
    """规划一张输出瓦片怎么拼出来（当前所有数据源均为 XYZ 网格）。"""
    return plan_xyz(source, z, x, y, fix_offset=fix_offset, precise=precise)


def plan_xyz(
    source: MapSource,
    z: int,
    x: int,
    y: int,
    *,
    fix_offset: bool = True,
    precise: bool = True,
) -> TilePlan:
    """标准 XYZ 网格（高德 / 腾讯 / 天地图）。"""
    source_level = min(z, source.max_level)
    scale = 2 ** (z - source_level)
    tile_px = float(TILE_SIZE * scale)
    limit = 2**source_level

    shift_x = 0.0
    shift_y = 0.0
    if fix_offset:
        lng = tile_x_to_lon(x + 0.5, z)
        lat = tile_y_to_lat(y + 0.5, z)
        d_lng, d_lat = datum_lnglat(lng, lat, source.datum, True)
        if d_lng != lng or d_lat != lat:
            shift_x = ((d_lng - lng) / 360) * TILE_SIZE * 2**z
            shift_y = merc_y(d_lat, z) - merc_y(lat, z)

    if not precise:
        shift_x = round(shift_x / tile_px) * tile_px
        shift_y = round(shift_y / tile_px) * tile_px

    want_x = TILE_SIZE * x + shift_x
    want_y = TILE_SIZE * y + shift_y

    tx = math.floor(want_x / tile_px)
    ty = math.floor(want_y / tile_px)

    offset_x = round(want_x - tx * tile_px)
    offset_y = round(want_y - ty * tile_px)
    if offset_x >= tile_px:
        offset_x -= int(tile_px)
        tx += 1
    elif offset_x < 0:
        offset_x += int(tile_px)
        tx -= 1
    if offset_y >= tile_px:
        offset_y -= int(tile_px)
        ty += 1
    elif offset_y < 0:
        offset_y += int(tile_px)
        ty -= 1

    tiles = [PlannedTile(wrap_x(tx, limit), ty, 0.0, 0.0)]
    if offset_x > 0:
        tiles.append(PlannedTile(wrap_x(tx + 1, limit), ty, tile_px, 0.0))
    if offset_y > 0:
        tiles.append(PlannedTile(wrap_x(tx, limit), ty + 1, 0.0, tile_px))
    if offset_x > 0 and offset_y > 0:
        tiles.append(PlannedTile(wrap_x(tx + 1, limit), ty + 1, tile_px, tile_px))

    passthrough = (
        len(tiles) == 1 and offset_x == 0 and offset_y == 0 and tile_px == TILE_SIZE
    )

    return TilePlan(
        z=z,
        x=x,
        y=y,
        source_level=source_level,
        grid="xyz",
        limit=float(limit),
        tile_px_x=tile_px,
        tile_px_y=tile_px,
        offset_x=offset_x,
        offset_y=offset_y,
        tiles=tuple(tiles),
        passthrough=passthrough,
    )


__all__ = [
    "GCJ_A",
    "GCJ_EE",
    "TILE_SIZE",
    "PlannedTile",
    "TilePlan",
    "datum_lnglat",
    "merc_y",
    "out_of_china",
    "plan_targets",
    "plan_tile",
    "source_urls",
    "tile_in_range",
    "tile_url",
    "tile_x_to_lon",
    "tile_y_to_lat",
    "transform_lat",
    "transform_lng",
    "wgs84_to_gcj02",
    "wrap_x",
]
