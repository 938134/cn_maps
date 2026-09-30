"""运行期状态 + 前端配置生成。

合并了原来的 runtime.py（选项 + 访问令牌 + 生成好的样式与前端模块）和
builder.py（生成注入前端的配置：MapLibre 样式、指纹、模块内容）。

选项一变就重建一份 runtime（指纹随之变化，浏览器缓存自动失效）；瓦片缓存挂在
TileService 上，跨选项变更复用。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import secrets
from typing import Any, Mapping

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_AUTO_RELOAD,
    CONF_DEBUG,
    CONF_DARKEN_IN_DARK_MODE,
    CONF_FIX_CHINA_OFFSET,
    CONF_KEYS,
    CONF_MAP_SOURCE,
    CONF_PRECISE_OFFSET,
    DATA_RUNTIME,
    DEFAULT_OPTIONS,
    DOMAIN,
    MODULE_PATH,
    NAME,
    TILES_PATH,
    ASSET_FILE,
)
from .projection import TILE_SIZE
from .sources import DEFAULT_SOURCE, MapSource, SOURCES, get_source
from .tiles import TileService, TileSettings

_LOGGER = logging.getLogger(__name__)

_TOKEN_STORAGE_KEY = f"{DOMAIN}.token"
_TOKEN_STORAGE_VERSION = 1

# hass.data[DOMAIN] 里的键
DATA_TILES = "tiles"

STYLE_SOURCE_ID = "cn-base"


# ============================================================ 配置生成（原 builder）

def normalize_options(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把选项补全、纠偏成规范形式（键齐全、类型正确）。"""
    merged: dict[str, Any] = dict(DEFAULT_OPTIONS)
    if raw:
        merged.update({key: value for key, value in raw.items() if value is not None})

    merged[CONF_MAP_SOURCE] = get_source(merged.get(CONF_MAP_SOURCE)).key

    keys = merged.get(CONF_KEYS) or {}
    merged[CONF_KEYS] = {
        str(name): str(value).strip()
        for name, value in dict(keys).items()
        if value and str(value).strip()
    }

    for flag in (
        CONF_FIX_CHINA_OFFSET,
        CONF_PRECISE_OFFSET,
        CONF_DARKEN_IN_DARK_MODE,
        CONF_DEBUG,
        CONF_AUTO_RELOAD,
    ):
        merged[flag] = bool(merged.get(flag, DEFAULT_OPTIONS[flag]))
    return merged


def api_key_for(options: Mapping[str, Any], source: MapSource | None = None) -> str:
    """取某个数据源要用的密钥（不需要密钥的源返回空串）。"""
    source = source or get_source(options.get(CONF_MAP_SOURCE))
    if not source.key_name:
        return ""
    return str((options.get(CONF_KEYS) or {}).get(source.key_name, ""))


def tile_template(fingerprint: str, token: str) -> str:
    """服务端瓦片接口的地址模板（相对路径，前端按需转成绝对地址）。"""
    return f"{TILES_PATH}/{fingerprint}/{{z}}/{{x}}/{{y}}.png?token={token}"


def module_url(fingerprint: str) -> str:
    """注入前端用的模块地址（带指纹，方便长缓存）。"""
    return f"{MODULE_PATH}/{fingerprint}/{ASSET_FILE}"


def build_style(
    options: Mapping[str, Any], *, tile_path: str, dark: bool = False
) -> dict[str, Any]:
    """一张 MapLibre 样式：单个栅格瓦片源，指向本集成的瓦片接口。"""
    source = get_source(options.get(CONF_MAP_SOURCE))
    layer: dict[str, Any] = {
        "id": STYLE_SOURCE_ID,
        "type": "raster",
        "source": STYLE_SOURCE_ID,
    }
    if dark and options.get(CONF_DARKEN_IN_DARK_MODE):
        layer["paint"] = {
            "raster-brightness-max": 0.62,
            "raster-contrast": 0.08,
            "raster-saturation": -0.35,
        }
    return {
        "version": 8,
        "name": f"{source.label}（{NAME}）",
        "sources": {
            STYLE_SOURCE_ID: {
                "type": "raster",
                "tiles": [tile_path],
                "tileSize": TILE_SIZE,
                "minzoom": 0,
                "maxzoom": source.max_zoom,
                "attribution": source.attribution,
            }
        },
        "layers": [layer],
    }


def build_styles(options: Mapping[str, Any], *, tile_path: str) -> dict[str, Any]:
    """亮色 + 暗色两张样式。"""
    return {
        "light": build_style(options, tile_path=tile_path, dark=False),
        "dark": build_style(options, tile_path=tile_path, dark=True),
    }


def compute_fingerprint(
    asset: str,
    options: Mapping[str, Any],
    integration_version: str,
    token: str = "",
) -> str:
    """配置指纹：集成版本 / 选项 / 脚本内容 / 访问令牌任一变化都会变。"""
    payload = "|".join(
        (
            integration_version,
            token,
            json.dumps(dict(options), sort_keys=True, ensure_ascii=False),
            asset,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def build_frontend_config(
    options: Mapping[str, Any],
    *,
    fingerprint: str,
    tile_path: str,
    integration_version: str,
) -> dict[str, Any]:
    """注入给前端脚本的配置对象。"""
    source = get_source(options.get(CONF_MAP_SOURCE))
    return {
        "version": integration_version,
        "fingerprint": fingerprint,
        "source": source.key,
        "sourceLabel": source.label,
        "tileTemplate": tile_path,
        "debug": bool(options.get(CONF_DEBUG)),
        "autoReload": bool(options.get(CONF_AUTO_RELOAD)),
        "styles": build_styles(options, tile_path=tile_path),
        # 工具栏用：所有可选数据源列表
        "sources": [
            {"key": s.key, "label": s.label} for s in SOURCES.values()
        ],
    }


def render_module(
    asset: str, config: Mapping[str, Any], integration_version: str
) -> str:
    """拼出要注入前端的模块内容：配置前缀 + 前端脚本原文。"""
    header = (
        f"/* 本文件由 Home Assistant 集成 {DOMAIN} v{integration_version} 生成，"
        "请勿手工修改；改集成选项即可。 */\n"
        f"window.__cnMapsConfigVersion = {json.dumps(config['fingerprint'])};\n"
        "window.__cnMapsConfig = "
        f"{json.dumps(dict(config), ensure_ascii=False, separators=(',', ':'))};\n"
    )
    return header + asset


# ============================================================ 运行期状态（原 runtime）

@dataclass(slots=True)
class CnMapsRuntime:
    """当前生效的一套配置。"""

    options: dict[str, Any]
    token: str
    fingerprint: str
    tile_path: str
    styles: dict[str, Any]
    module_body: bytes
    module_url: str
    tiles: TileService
    settings: TileSettings

    @property
    def source_label(self) -> str:
        return get_source(self.options[CONF_MAP_SOURCE]).label

    @property
    def has_key(self) -> bool:
        source = get_source(self.options[CONF_MAP_SOURCE])
        if not source.key_name:
            return True
        return bool((self.options.get(CONF_KEYS) or {}).get(source.key_name))


def build_runtime(
    hass: HomeAssistant,
    options: dict[str, Any] | None,
    token: str,
    asset: str,
    integration_version: str,
) -> CnMapsRuntime:
    """按选项生成一套运行期对象。"""
    normalized = normalize_options(options)
    source = get_source(normalized[CONF_MAP_SOURCE])

    fingerprint = compute_fingerprint(
        asset, normalized, integration_version, token
    )
    tile_path = tile_template(fingerprint, token)
    config = build_frontend_config(
        normalized,
        fingerprint=fingerprint,
        tile_path=tile_path,
        integration_version=integration_version,
    )
    module_body = render_module(asset, config, integration_version).encode("utf-8")

    settings = TileSettings(
        fingerprint=fingerprint,
        source=source,
        api_key=api_key_for(normalized, source),
        fix_offset=normalized[CONF_FIX_CHINA_OFFSET],
        precise=normalized[CONF_PRECISE_OFFSET],
        debug=normalized[CONF_DEBUG],
    )

    return CnMapsRuntime(
        options=normalized,
        token=token,
        fingerprint=fingerprint,
        tile_path=tile_path,
        styles=config["styles"],
        module_body=module_body,
        module_url=module_url(fingerprint),
        tiles=get_tile_service(hass),
        settings=settings,
    )


def current(hass: HomeAssistant) -> CnMapsRuntime | None:
    """当前生效的 runtime（没配置好就是 None）。"""
    return (hass.data.get(DOMAIN) or {}).get(DATA_RUNTIME)


def get_tile_service(hass: HomeAssistant) -> TileService:
    """进程级共享的瓦片服务（缓存跟着它走）。"""
    store = hass.data.setdefault(DOMAIN, {})
    service: TileService | None = store.get(DATA_TILES)
    if service is None:
        service = TileService(hass)
        store[DATA_TILES] = service
    return service


async def async_get_or_create_token(hass: HomeAssistant) -> str:
    """本安装专属的访问令牌，存 .storage（只用来挡住乱刷，不是隐私密钥）。"""
    store: Store = Store(hass, _TOKEN_STORAGE_VERSION, _TOKEN_STORAGE_KEY)
    data = await store.async_load()
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        token = secrets.token_hex(16)
        await store.async_save({"token": token})
    return token


__all__ = [
    "CnMapsRuntime",
    "async_get_or_create_token",
    "api_key_for",
    "build_frontend_config",
    "build_runtime",
    "build_style",
    "build_styles",
    "compute_fingerprint",
    "current",
    "get_tile_service",
    "module_url",
    "normalize_options",
    "render_module",
    "tile_template",
]
