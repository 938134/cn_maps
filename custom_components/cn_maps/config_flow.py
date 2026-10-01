"""配置流 + 选项流 + 表单 schema + Key 校验。

选项流只有一页：数据源 + 天地图 Key + 纠偏开关。
天地图 Key 统一配置，切换到高德/腾讯/百度时不会丢失。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

import voluptuous as vol

from aiohttp import ClientError, ClientTimeout

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_FIX_CHINA_OFFSET,
    CONF_KEYS,
    CONF_MAP_SOURCE,
    CONF_PRECISE_OFFSET,
    DEFAULT_OPTIONS,
    DOMAIN,
    NAME,
)
from .runtime import normalize_options
from .sources import SOURCES, get_source

_LOGGER = logging.getLogger(__name__)

# ============================================================ 表单 schema

_SOURCE_OPTIONS = [
    selector.SelectOptionDict(value=source.key, label=source.label)
    for source in SOURCES.values()
]

# 天地图 Key 的标识（唯一需要 Key 的数据源）
_TIANDITU_KEY = "tianditu"

# Key 在表单里用密码框显示（带"眼睛"图标可临时查看）；
# 老版本 HA 没有 PASSWORD 类型时退回明文，保证不崩。
_KEY_TEXT_TYPE = getattr(
    selector.TextSelectorType, "PASSWORD", selector.TextSelectorType.TEXT
)


def _options_schema(options: Mapping[str, Any] | None) -> vol.Schema:
    """统一的表单：数据源 + 天地图 Key + 纠偏开关。"""
    options = options or {}
    current = get_source(options.get(CONF_MAP_SOURCE))
    existing_key = str((options.get(CONF_KEYS) or {}).get(_TIANDITU_KEY, ""))

    return vol.Schema({
        vol.Required(CONF_MAP_SOURCE, default=current.key): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=_SOURCE_OPTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_API_KEY, default=existing_key): selector.TextSelector(
            selector.TextSelectorConfig(type=_KEY_TEXT_TYPE)
        ),
        vol.Optional(CONF_FIX_CHINA_OFFSET, default=bool(
            options.get(CONF_FIX_CHINA_OFFSET, True)
        )): selector.BooleanSelector(),
        vol.Optional(CONF_PRECISE_OFFSET, default=bool(
            options.get(CONF_PRECISE_OFFSET, True)
        )): selector.BooleanSelector(),
    })


def _save_options(user_input: dict, base: Mapping[str, Any] | None = None) -> dict:
    """把表单输入转成规范选项。"""
    base = dict(base or {})
    source = get_source(user_input.get(CONF_MAP_SOURCE))
    key = str(user_input.get(CONF_API_KEY) or "").strip()

    keys = dict(base.get(CONF_KEYS) or {})
    if key:
        keys[_TIANDITU_KEY] = key
    else:
        keys.pop(_TIANDITU_KEY, None)

    base[CONF_MAP_SOURCE] = source.key
    base[CONF_KEYS] = keys
    base[CONF_FIX_CHINA_OFFSET] = bool(user_input.get(CONF_FIX_CHINA_OFFSET, True))
    base[CONF_PRECISE_OFFSET] = bool(user_input.get(CONF_PRECISE_OFFSET, True))
    return normalize_options(base)


# ============================================================ Key 校验

_TIMEOUT = ClientTimeout(total=10)
_TIANDITU_PROBE = "https://t0.tianditu.gov.cn/DataServer?T=vec_w&X=0&Y=0&L=1&tk={key}"
_INVALID_MARKERS = ("301001", "301002", "301003", "非法", "invalid", "Invalid")


async def _validate_tianditu_key(
    hass: HomeAssistant, source_key: str, key: str
) -> str | None:
    """选天地图时校验 Key；选其它数据源时跳过。"""
    source = get_source(source_key)
    if not source.needs_key:
        return None

    key = (key or "").strip()
    if not key:
        return "key_required"

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
            key = str(user_input.get(CONF_API_KEY) or "").strip()
            error = await _validate_tianditu_key(
                self.hass, user_input.get(CONF_MAP_SOURCE, ""), key
            )
            if error:
                errors[CONF_API_KEY] = error
            else:
                options = _save_options(user_input)
                source = get_source(options[CONF_MAP_SOURCE])
                return self.async_create_entry(
                    title=f"{NAME} · {source.label}", data={}, options=options
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_options_schema(None),
            errors=errors,
            last_step=True,
        )


# ============================================================ 选项流（单页）

class CnMapsOptionsFlow(OptionsFlow):
    """改数据源、填天地图 Key、调纠偏。一页搞定。"""

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            key = str(user_input.get(CONF_API_KEY) or "").strip()
            error = await _validate_tianditu_key(
                self.hass, user_input.get(CONF_MAP_SOURCE, ""), key
            )
            if error:
                errors[CONF_API_KEY] = error
            else:
                options = _save_options(user_input, self.config_entry.options)
                return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self.config_entry.options),
            errors=errors,
        )


__all__ = ["CnMapsConfigFlow", "CnMapsOptionsFlow"]
