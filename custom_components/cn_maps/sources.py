"""底图数据源注册表。

支持高德路网/卫星、腾讯路网/卫星、天地图矢量/影像、百度路网，共 7 种。
高德/腾讯/百度不需要 Key；天地图需要免费申请的 tk。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .projection import BD_LEVEL_OFFSET

# 各家的 Referer
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
    key_name: str | None = None

    @property
    def needs_key(self) -> bool:
        return self.key_name is not None

    @property
    def max_zoom(self) -> int:
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
        # 腾讯路网：realtimerender 接口，y 上下翻转
        layers=(
            "https://rt{s}.map.gtimg.com/realtimerender"
            "?z={z}&x={x}&y={-y}&type=vector&styleid=1",
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
        layers=("https://p{s}.map.gtimg.com/sateTiles/{z}/{sx}/{sy}/{x}_{-y}.jpg",),
    ),
    "tianditu-vector": MapSource(
        key="tianditu-vector",
        label="天地图 · 矢量",
        datum="wgs84",
        grid="xyz",
        max_level=18,
        subdomains=("0", "1", "2", "3", "4", "5", "6", "7"),
        referer=_REFERER_TIANDITU,
        attribution=_ATTR_TIANDITU,
        key_name="tianditu",
        layers=(
            "https://t{s}.tianditu.gov.cn/DataServer?T=vec_w&X={x}&Y={y}&L={z}&tk={tk}",
            "https://t{s}.tianditu.gov.cn/DataServer?T=cva_w&X={x}&Y={y}&L={z}&tk={tk}",
        ),
    ),
    "tianditu-image": MapSource(
        key="tianditu-image",
        label="天地图 · 影像",
        datum="wgs84",
        grid="xyz",
        max_level=18,
        subdomains=("0", "1", "2", "3", "4", "5", "6", "7"),
        referer=_REFERER_TIANDITU,
        attribution=_ATTR_TIANDITU,
        key_name="tianditu",
        layers=(
            "https://t{s}.tianditu.gov.cn/DataServer?T=img_w&X={x}&Y={y}&L={z}&tk={tk}",
            "https://t{s}.tianditu.gov.cn/DataServer?T=cia_w&X={x}&Y={y}&L={z}&tk={tk}",
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
        layers=(
            "https://online{s}.map.bdimg.com/tile/"
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


__all__ = [
    "DEFAULT_SOURCE",
    "SOURCES",
    "SOURCE_KEYS",
    "MapSource",
    "get_source",
]