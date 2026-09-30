"""配置流 + 选项流 + 表单 schema + Key 校验。

合并了原来的 config_flow.py、options_flow.py、schemas.py、validation.py 四个文件。
选项流只有一页表单（数据源 + Key + 纠偏），高级参数走默认值。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Mapping

import voluptuous as vol

from aiohttp import ClientError, ClientTimeout

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BASIC_FIELDS,
    CONF_API_KEY,
    CONF_FIX_CHINA_OFFSET,
    CONF_KEYS,
    CONF_MAP_SOURCE,
    CONF_PRECISE_OFFSET,
    DEFAULT_OPTIONS,
    DOMAIN,
    NAME,
)
from .runtime import api_key_for, normalize_options
from .sources import SOURCES, get_source, MapSource

_LOGGER = logging.getLogger(__name__)

# ============================================================ 表单 schema

_SOURCE_OPTIONS = [
    selector.SelectOptionDict(value=source.key, label=source.label)
    for source in SOURCES.values()
]


def _source_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_SOURCE_OPTIONS,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _selector_for(key: str) -> Any:
    """布尔开关。"""
    return selector.BooleanSelector()


def _field(key: str, options: Mapping[str, Any]) -> Any:
    value = options.get(key, DEFAULT_OPTIONS.get(key))
    return vol.Optional(key, default=bool(value))


def form_schema(
    options: Mapping[str, Any] | None,
    fields: Iterable[str] = (),
    *,
    include_source: bool = True,
) -> vol.Schema:
    """数据源 + API Key + 指定的其它字段。"""
    options = options or {}
    current = get_source(options.get(CONF_MAP_SOURCE))

    data: dict[Any, Any] = {}
    if include_source:
        data[vol.Required(CONF_MAP_SOURCE, default=current.key)] = _source_selector()
        data[vol.Optional(CONF_API_KEY, default=api_key_for(options, current))] = (
            selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            )
        )
    for key in fields:
        if key == CONF_MAP_SOURCE:
            continue
        data[_field(key, options)] = _selector_for(key)
    return vol.Schema(data)


# ============================================================ Key 校验

_TIMEOUT = ClientTimeout(total=10)
_TIANDITU_PROBE = "https://t0.tianditu.gov.cn/DataServer?T=vec_w&X=0&Y=0&L=1&tk={key}"
_INVALID_MARKERS = ("301001", "301002", "301003", "非法", "invalid", "Invalid")


async def async_validate_key(
    hass: HomeAssistant, source: MapSource, key: str
) -> str | None:
    """返回错误码（None = 通过）。"""
    if not source.needs_key:
        return None

    key = (key or "").strip()
    if not key:
        return "key_required"

    if source.key_name == "tianditu":
        return await _check_tianditu(hass, key)

    return None


async def _check_tianditu(hass: HomeAssistant, key: str) -> str | None:
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            _TIANDITU_PROBE.format(key=key), timeout=_TIMEOUT
        ) as resp:
            if resp.status != 200:
                return "cannot_connect"
            content_type = resp.headers.get("Content-Type", "")
            body = await resp.read()
    except (ClientError, asyncio.TimeoutError) as err:
        _LOGGER.warning("cn_maps: 校验天地图 Key 时连不上服务器（%s），先放行", err)
        return None

    if content_type.startswith("image/"):
        return None

    text = body[:512].decode("utf-8", errors="ignore")
    if any(marker in text for marker in _INVALID_MARKERS):
        _LOGGER.warning("cn_maps: 天地图 Key 校验失败：%s", text.strip()[:200])
        return "invalid_key"
    if not text.strip():
        return "cannot_connect"
    return None


# ============================================================ 配置流

class CnMapsConfigFlow(ConfigFlow, domain=DOMAIN):
    """把 HA 内置地图的底图换成国内地图源。"""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry) -> "CnMapsOptionsFlow":  # noqa: ARG004
        return CnMapsOptionsFlow()

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            source = get_source(user_input.get(CONF_MAP_SOURCE))
            key = str(user_input.get(CONF_API_KEY) or "").strip()

            error = await async_validate_key(self.hass, source, key)
            if error:
                errors[CONF_API_KEY] = error
            else:
                options = normalize_options(
                    {
                        CONF_MAP_SOURCE: source.key,
                        CONF_KEYS: (
                            {source.key_name: key} if source.key_name and key else {}
                        ),
                    }
                )
                return self.async_create_entry(
                    title=f"{NAME} · {source.label}", data={}, options=options
                )

        return self.async_show_form(
            step_id="user",
            data_schema=form_schema(None),
            errors=errors,
            last_step=True,
        )


# ============================================================ 选项流（单页）

class CnMapsOptionsFlow(OptionsFlow):
    """改数据源、填 Key、调纠偏。一页搞定。"""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        self._options = normalize_options(self.config_entry.options)
        errors: dict[str, str] = {}

        if user_input is not None:
            source = get_source(user_input.get(CONF_MAP_SOURCE))
            key = str(user_input.get(CONF_API_KEY) or "").strip()

            error = await async_validate_key(self.hass, source, key)
            if error:
                errors[CONF_API_KEY] = error
            else:
                keys = dict(self._options.get(CONF_KEYS) or {})
                if source.key_name:
                    if key:
                        keys[source.key_name] = key
                    else:
                        keys.pop(source.key_name, None)
                self._options.update(
                    {
                        CONF_MAP_SOURCE: source.key,
                        CONF_KEYS: keys,
                        CONF_FIX_CHINA_OFFSET: bool(
                            user_input.get(CONF_FIX_CHINA_OFFSET, True)
                        ),
                        CONF_PRECISE_OFFSET: bool(
                            user_input.get(CONF_PRECISE_OFFSET, True)
                        ),
                    }
                )
                return self.async_create_entry(
                    data=normalize_options(self._options)
                )

        return self.async_show_form(
            step_id="init",
            data_schema=form_schema(self._options, (*BASIC_FIELDS, CONF_API_KEY)),
            errors=errors,
        )


__all__ = ["CnMapsConfigFlow", "CnMapsOptionsFlow"]
