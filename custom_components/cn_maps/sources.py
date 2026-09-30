"""底图数据源注册表。

每个数据源描述四件事：

* ``datum``     瓦片用的坐标系：``gcj02``（高德/腾讯）、``bd09``（百度）、
                ``wgs84``（天地图，CGCS2000 与 WGS84 差异可忽略）
* ``grid``      瓦片网格：``xyz``（标准 Web Mercator）或 ``bd09mc``（百度自有网格）
* ``max_level`` 该服务实际能给出的最高瓦片级别，超过就用低一级放大，不会白屏
* ``layers``    瓦片地址模板，按顺序叠放（后者画在上层）
                ``{s}`` 服务器号 ``{x}{y}{z}`` 瓦片号 ``{-y}`` 上下翻转的 y
                ``{sx}{sy}`` 腾讯卫星目录号 ``{tk}`` 天地图 Key

地址均为 2026-09 实测可用的形式；腾讯（y 翻转）与百度（椭球网格）都用真实瓦片
核过地理位置。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .projection import BD_LEVEL_OFFSET

# 各家的 Referer：服务端代取瓦片时带上，减少被拒的概率。
_REFERER_AMAP = "https://www.amap.com/"
_REFERER_TENCENT = "https://map.qq.com/"
_REFERER_TIANDITU = "https://www.tianditu.gov.cn/"
_REFERER_BAIDU = "https://map.baidu.com/"

_ATTR_AMAP = '&copy; <a href="https://www.amap.com/" target="_blank" rel="noopener noreferrer">高德地图</a>'
_ATTR_TENCENT = '&copy; <a href="https://map.qq.com/" target="_blank" rel="noopener noreferrer">腾讯地图</a>'
_ATTR_TIANDITU = '&copy; <a href="https://www.tianditu.gov.cn/" target="_blank" rel="noopener noreferrer">天地图</a>'
_ATTR_BAIDU = '&copy; <a href="https://map.baidu.com/" target="_blank" rel="noopener noreferrer">百度地图</a>'


@dataclass(frozen=True, slots=True)
class MapSource:
    """一个底图数据源。"""

    key: str
    label: str
    datum: str  # wgs84 | gcj02 | bd09
    grid: str  # xyz | bd09mc
    max_level: int
    subdomains: tuple[str, ...]
    attribution: str
    layers: tuple[str, ...]
    referer: str = ""
    # 需要密钥的数据源：key_name 是密钥的标识（同时是 CONF_KEYS 里的键名）
    key_name: str | None = None
    key_label: str = ""
    key_hint: str = ""
    key_url: str = ""

    @property
    def needs_key(self) -> bool:
        return self.key_name is not None

    @property
    def max_zoom(self) -> int:
        """喂给 MapLibre raster source 的 ``maxzoom``（按输出网格的口径）。

        百度网格比标准 XYZ 粗 0.74 级（L18 ≈ z17.26），这里向上取整，让
        MapLibre 直接请求我们合成的 z18，而不是请求 z17 再放大两次。
        """
        if self.grid == "bd09mc":
            return math.ceil(self.max_level - BD_LEVEL_OFFSET)
        return self.max_level


SOURCES: dict[str, MapSource] = {
    "amap-road": MapSource(
        key="amap-road",
        label="高德 · 路网图",
        datum="gcj02",
        grid="xyz",
        max_level=18,
        subdomains=("1", "2", "3", "4"),
        referer=_REFERER_AMAP,
        attribution=_ATTR_AMAP,
        layers=(
            "https://webrd0{s}.is.autonavi.com/appmaptile"
            "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
        ),
    ),
    "amap-satellite": MapSource(
        key="amap-satellite",
        label="高德 · 卫星影像",
        datum="gcj02",
        grid="xyz",
        max_level=18,
        subdomains=("1", "2", "3", "4"),
        referer=_REFERER_AMAP,
        attribution=_ATTR_AMAP,
        layers=(
            # 第 0 层：卫星影像（不透明）；第 1 层：路网 + 注记（透明，叠在上面）
            "https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}",
            "https://webst0{s}.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}",
        ),
    ),
    "tencent-road": MapSource(
        key="tencent-road",
        label="腾讯 · 路网图",
        datum="gcj02",
        grid="xyz",
        max_level=18,
        subdomains=("0", "1", "2", "3"),
        referer=_REFERER_TENCENT,
        attribution=_ATTR_TENCENT,
        # 腾讯的 y 是上下翻转的：{-y} = 2^z - 1 - y
        layers=(
            "https://rt{s}.map.gtimg.com/tile"
            "?z={z}&x={x}&y={-y}&type=vector&styleid=3",
        ),
    ),
    "tencent-satellite": MapSource(
        key="tencent-satellite",
        label="腾讯 · 卫星影像",
        datum="gcj02",
        grid="xyz",
        max_level=18,
        subdomains=("0", "1", "2", "3"),
        referer=_REFERER_TENCENT,
        attribution=_ATTR_TENCENT,
        # {sx}=x>>4，{sy}=(2^z-y)>>4，文件名同样用翻转后的 y
        layers=("https://p{s}.map.gtimg.com/sateTiles/{z}/{sx}/{sy}/{x}_{-y}.jpg",),
    ),
    "tianditu-vector": MapSource(
        key="tianditu-vector",
        label="天地图 · 矢量（需 Key）",
        datum="wgs84",
        grid="xyz",
        max_level=18,
        subdomains=("0", "1", "2", "3", "4", "5", "6", "7"),
        referer=_REFERER_TIANDITU,
        attribution=_ATTR_TIANDITU,
        key_name="tianditu",
        key_label="天地图 Key（tk）",
        key_hint=(
            "在 console.tianditu.gov.cn 免费申请，应用类型选「浏览器端」。"
            "本集成由 HA 服务端代取瓦片，Key 不会发给浏览器。"
        ),
        key_url="https://console.tianditu.gov.cn/api/key",
        # vec 底图 + cva 注记
        layers=(
            "https://t{s}.tianditu.gov.cn/DataServer?T=vec_w&X={x}&Y={y}&L={z}&tk={tk}",
            "https://t{s}.tianditu.gov.cn/DataServer?T=cva_w&X={x}&Y={y}&L={z}&tk={tk}",
        ),
    ),
    "tianditu-image": MapSource(
        key="tianditu-image",
        label="天地图 · 影像（需 Key）",
        datum="wgs84",
        grid="xyz",
        max_level=18,
        subdomains=("0", "1", "2", "3", "4", "5", "6", "7"),
        referer=_REFERER_TIANDITU,
        attribution=_ATTR_TIANDITU,
        key_name="tianditu",
        key_label="天地图 Key（tk）",
        key_hint="与「天地图 · 矢量」共用同一个 Key。",
        key_url="https://console.tianditu.gov.cn/api/key",
        layers=(
            "https://t{s}.tianditu.gov.cn/DataServer?T=img_w&X={x}&Y={y}&L={z}&tk={tk}",
            "https://t{s}.tianditu.gov.cn/DataServer?T=cia_w&X={x}&Y={y}&L={z}&tk={tk}",
        ),
    ),
    "tianditu-terrain": MapSource(
        key="tianditu-terrain",
        label="天地图 · 地形（需 Key）",
        datum="wgs84",
        grid="xyz",
        max_level=14,
        subdomains=("0", "1", "2", "3", "4", "5", "6", "7"),
        referer=_REFERER_TIANDITU,
        attribution=_ATTR_TIANDITU,
        key_name="tianditu",
        key_label="天地图 Key（tk）",
        key_hint="与「天地图 · 矢量」共用同一个 Key。地形图最高 14 级。",
        key_url="https://console.tianditu.gov.cn/api/key",
        layers=(
            "https://t{s}.tianditu.gov.cn/DataServer?T=ter_w&X={x}&Y={y}&L={z}&tk={tk}",
            "https://t{s}.tianditu.gov.cn/DataServer?T=cta_w&X={x}&Y={y}&L={z}&tk={tk}",
        ),
    ),
    "baidu-road": MapSource(
        key="baidu-road",
        label="百度 · 路网图",
        datum="bd09",
        grid="bd09mc",
        max_level=18,
        subdomains=("0", "1", "2", "3"),
        referer=_REFERER_BAIDU,
        attribution=_ATTR_BAIDU,
        # x/y 是百度自有网格的瓦片号（可为负），不是标准 XYZ
        layers=(
            "https://maponline{s}.bdimg.com/tile"
            "?qt=tile&x={x}&y={y}&z={z}&styles=pl&scaler=1&p=1",
        ),
    ),
}

DEFAULT_SOURCE = "amap-road"

SOURCE_KEYS: tuple[str, ...] = tuple(SOURCES)


def get_source(key: str | None) -> MapSource:
    """按 key 取数据源，非法 key 回落到默认源。"""
    if key and key in SOURCES:
        return SOURCES[key]
    return SOURCES[DEFAULT_SOURCE]


def key_label_for(key: str | None) -> str:
    """该数据源需要的密钥名（不需要则空串）。"""
    source = get_source(key)
    return source.key_name or ""


def key_hint_for(key: str | None) -> str:
    return get_source(key).key_hint


__all__ = [
    "DEFAULT_SOURCE",
    "SOURCES",
    "SOURCE_KEYS",
    "MapSource",
    "get_source",
    "key_hint_for",
    "key_label_for",
]
