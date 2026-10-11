"""底图数据源注册表。

支持 HA 自带地图，以及高德路网/卫星、腾讯卫星、天地图矢量/影像，共 6 种。
高德/腾讯不需要 Key；天地图需要免费申请的 tk；HA 自带地图不经过本集成的
瓦片代理（样式透传）。
"""

from __future__ import annotations

from dataclasses import dataclass

_REFERER_AMAP = "https://www.amap.com/"
_REFERER_TENCENT = "https://map.qq.com/"
_REFERER_TIANDITU = "https://www.tianditu.gov.cn/"

_ATTR_AMAP = '&copy; <a href="https://www.amap.com/" target="_blank" rel="noopener noreferrer">高德地图</a>'
_ATTR_TENCENT = '&copy; <a href="https://map.qq.com/" target="_blank" rel="noopener noreferrer">腾讯地图</a>'
_ATTR_TIANDITU = '&copy; <a href="https://www.tianditu.gov.cn/" target="_blank" rel="noopener noreferrer">天地图</a>'


@dataclass(frozen=True, slots=True)
class MapSource:
    """一个底图数据源。"""

    key: str
    label: str
    datum: str  # wgs84 | gcj02
    grid: str  # xyz
    max_level: int
    subdomains: tuple[str, ...]
    attribution: str
    layers: tuple[str, ...]
    referer: str = ""
    key_name: str | None = None
    # True = HA 自带地图：不代理任何瓦片，样式直接透传给 HA 自己的底图
    builtin: bool = False

    @property
    def needs_key(self) -> bool:
        return self.key_name is not None

    @property
    def max_zoom(self) -> int:
        return self.max_level


SOURCES: dict[str, MapSource] = {
    "ha-builtin": MapSource(
        key="ha-builtin",
        label="HA 自带地图",
        datum="wgs84",
        grid="xyz",
        max_level=22,
        subdomains=(),
        attribution="",
        layers=(),
        builtin=True,
    ),
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
