"""cn_maps —— 把 Home Assistant 内置地图的底图换成国内地图源。

集成不提供任何实体，只做三件事：

1. 注册服务端瓦片接口：抓上游瓦片、按数据源的坐标系做纠偏与拼合、内存缓存；
2. 注册注入前端的模块：它只把 HA 内置的 ``/static/map/{light,dark}.json``
   换成项目生成的样式（样式里的瓦片指向本集成），客户端不再做坐标换算；
3. 选项一变就重建配置并重新注入，前端侦测到版本变化会自己刷新一次。

还注册了 ``cn_maps.set_source`` 服务，供前端工具栏快速切换地图源。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    ASSET_DIR,
    ASSET_FILE,
    DATA_MODULE_URL,
    DATA_RUNTIME,
    DATA_VIEWS_REGISTERED,
    DOMAIN,
    NAME,
)
from .runtime import (
    CnMapsRuntime,
    async_get_or_create_token,
    build_runtime,
)
from .sources import SOURCES, get_source
from .views import VIEW_CLASSES

try:  # pragma: no cover - 只有在极旧/裁剪过的 HA 上才会 ImportError
    from homeassistant.components.frontend import (
        add_extra_js_url,
        remove_extra_js_url,
    )
except ImportError:  # pragma: no cover
    add_extra_js_url = None  # type: ignore[assignment]
    remove_extra_js_url = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# cn_maps.set_source 服务 schema
SET_SOURCE_SCHEMA = vol.Schema(
    {
        vol.Required("source"): vol.In([s.key for s in SOURCES.values()]),
    }
)


def _integration_version() -> str:
    try:
        manifest = json.loads(
            (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
        )
        return str(manifest.get("version", "0"))
    except (OSError, ValueError):  # pragma: no cover
        return "0"


INTEGRATION_VERSION = _integration_version()


def _read_asset() -> str:
    return (Path(__file__).parent / ASSET_DIR / ASSET_FILE).read_text(encoding="utf-8")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """只为了让 CONFIG_SCHEMA 生效；一切配置都在 UI 里。"""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """设置一个实例。"""
    hass.data.setdefault(DOMAIN, {})
    _async_register_views(hass)

    token = await async_get_or_create_token(hass)
    runtime = build_runtime(
        hass, dict(entry.options), token, _read_asset(), INTEGRATION_VERSION
    )
    hass.data[DOMAIN][DATA_RUNTIME] = runtime
    _apply_frontend(hass, runtime)

    entry.add_update_listener(_async_options_updated)

    # 注册 cn_maps.set_source 服务：前端工具栏用
    if not hass.services.has_service(DOMAIN, "set_source"):
        hass.services.async_register(
            DOMAIN, "set_source", _async_set_source, schema=SET_SOURCE_SCHEMA
        )

    _LOGGER.info(
        "%s 已启用：底图=%s，坐标纠偏=%s，精确纠偏=%s%s",
        NAME,
        runtime.source_label,
        "开" if runtime.settings.fix_offset else "关",
        "开" if runtime.settings.precise else "关",
        "" if runtime.has_key else "（当前数据源没有填 Key，可能取不到瓦片）",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载：摘掉注入的前端模块（视图留着，没有 runtime 时会返回空瓦片）。"""
    store = hass.data.get(DOMAIN) or {}
    url = store.pop(DATA_MODULE_URL, None)
    if url:
        _remove_frontend(hass, url)
    store.pop(DATA_RUNTIME, None)

    # 只有本集成还在 hass.data 里时才摘服务（卸载最后一个实例）
    hass.services.async_remove(DOMAIN, "set_source")

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项保存后：重建运行期对象并重新注入。"""
    store = hass.data.setdefault(DOMAIN, {})
    previous: CnMapsRuntime | None = store.get(DATA_RUNTIME)
    token = (
        previous.token if previous is not None else await async_get_or_create_token(hass)
    )
    runtime = build_runtime(
        hass, dict(entry.options), token, _read_asset(), INTEGRATION_VERSION
    )
    store[DATA_RUNTIME] = runtime
    _apply_frontend(hass, runtime)
    _LOGGER.info(
        "%s 设置已更新：底图=%s（指纹 %s）",
        NAME,
        runtime.source_label,
        runtime.fingerprint,
    )


async def _async_set_source(hass: HomeAssistant, call: ServiceCall) -> None:
    """前端工具栏调用：切换地图源。"""
    source_key = call.data["source"]
    source = get_source(source_key)
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        _LOGGER.warning("%s: 没有配置实例，无法切换", NAME)
        return
    entry = entries[0]
    options = dict(entry.options)
    options["map_source"] = source.key
    # 触发 _async_options_updated 重建 + 重新注入
    hass.config_entries.async_update_entry(entry, options=options)
    _LOGGER.info("%s: 工具栏切换底图 -> %s", NAME, source.label)


def _async_register_views(hass: HomeAssistant) -> None:
    store = hass.data.setdefault(DOMAIN, {})
    if store.get(DATA_VIEWS_REGISTERED):
        return
    for view_class in VIEW_CLASSES:
        hass.http.register_view(view_class(hass))
    store[DATA_VIEWS_REGISTERED] = True


def _apply_frontend(hass: HomeAssistant, runtime: CnMapsRuntime) -> None:
    store = hass.data.setdefault(DOMAIN, {})
    previous = store.get(DATA_MODULE_URL)
    if previous == runtime.module_url:
        return
    if previous:
        _remove_frontend(hass, previous)
    if add_extra_js_url is None:  # pragma: no cover
        _LOGGER.error("%s：当前 Home Assistant 不支持注入前端模块", NAME)
        return
    try:
        add_extra_js_url(hass, runtime.module_url)
    except Exception as err:  # noqa: BLE001 - 不让注入失败拖垮整个集成
        _LOGGER.error("%s：注册前端模块失败：%s", NAME, err)
        return
    store[DATA_MODULE_URL] = runtime.module_url


def _remove_frontend(hass: HomeAssistant, url: str) -> None:
    if remove_extra_js_url is None:  # pragma: no cover
        return
    try:
        remove_extra_js_url(hass, url)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("%s：摘除前端模块 %s 失败：%s", NAME, url, err)


__all__ = ["async_setup", "async_setup_entry", "async_unload_entry"]
