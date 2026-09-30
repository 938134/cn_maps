"""HTTP 视图。

* ``/cn_maps/frontend/<指纹>/cn-maps.js``  注入前端的模块（配置 + 脚本）
* ``/cn_maps/tiles/<指纹>/<z>/<x>/<y>.png`` 服务端瓦片接口（MapLibre/Leaflet 直接取）
* ``/cn_maps/style.json``                  样式调试接口（需登录，给人看）

瓦片接口沿用 HA 官方 ``map_tiles`` 组件的思路：**不要求鉴权、用 URL 令牌兜底**。
原因是瓦片请求由 MapLibre 的 worker / Leaflet 的 ``<img>`` 发出，拿不到
``Authorization`` 头；令牌只用于防止别人把你家实例当免费瓦片代理乱刷，不保护
任何隐私数据（地图瓦片本身就是公开数据）。
"""

from __future__ import annotations

import hmac
import logging
import struct
import zlib

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    MODULE_URL_TEMPLATE,
    STYLE_PATH,
    TILES_PATH,
    VIEW_NAME,
)
from .runtime import CnMapsRuntime, current

_LOGGER = logging.getLogger(__name__)

_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
_CORS = {"Access-Control-Allow-Origin": "*"}


def _blank_png(size: int = 1) -> bytes:
    """手工拼一张全透明的 PNG（不依赖 Pillow）。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x00\x00\x00\x00" * size for _ in range(size))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


_BLANK = _blank_png()
_BLANK_HEADERS = {
    "Cache-Control": "public, max-age=60",
    **_CORS,
}


def _runtime(hass: HomeAssistant) -> CnMapsRuntime | None:
    return current(hass)


class CnMapsModuleView(HomeAssistantView):
    """注入前端的模块：配置前缀 + 前端脚本原文。"""

    url = MODULE_URL_TEMPLATE
    name = VIEW_NAME
    # add_extra_js_url 是以 <script type="module"> 加载的，带不了鉴权头
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, fingerprint: str) -> web.Response:
        runtime = _runtime(self._hass)
        if runtime is None or not hmac.compare_digest(fingerprint, runtime.fingerprint):
            return web.Response(status=404, text="not found")
        return web.Response(
            body=runtime.module_body,
            content_type="application/javascript",
            headers={**_IMMUTABLE, **_CORS},
        )


class CnMapsTileView(HomeAssistantView):
    """服务端瓦片接口。"""

    url = f"{TILES_PATH}/{{fingerprint}}/{{z}}/{{x}}/{{y}}.png"
    name = "cn_maps:tiles"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(
        self,
        request: web.Request,
        fingerprint: str,
        z: str,
        x: str,
        y: str,
    ) -> web.Response:
        runtime = _runtime(self._hass)
        if runtime is None:
            return self._blank()

        # 指纹对不上（浏览器还拿着旧样式的缓存）：回透明图，别报 404，
        # 否则 HA 的 MapLibre 会走「瓦片被拒 -> 刷新令牌」的恢复逻辑刷屏
        if not hmac.compare_digest(fingerprint, runtime.fingerprint):
            return self._blank()

        token = request.query.get("token") or ""
        if not hmac.compare_digest(token, runtime.token):
            return web.Response(status=403, text="invalid token")

        try:
            tile_z = int(z)
            tile_x = int(_strip_suffix(x))
            tile_y = int(_strip_suffix(y))
        except (TypeError, ValueError):
            return self._blank()

        result = await runtime.tiles.async_get_tile(
            runtime.settings, tile_z, tile_x, tile_y
        )
        if result is None:
            return self._blank()

        data, content_type = result
        return web.Response(
            body=data,
            content_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                **_CORS,
            },
        )

    @staticmethod
    def _blank() -> web.Response:
        return web.Response(
            body=_BLANK, content_type="image/png", headers=_BLANK_HEADERS
        )


class CnMapsStyleView(HomeAssistantView):
    """样式调试接口：浏览器里查看当前生成的 MapLibre 样式。"""

    url = STYLE_PATH
    name = "cn_maps:style"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        runtime = _runtime(self._hass)
        if runtime is None:
            return self.json_message("cn_maps 还没配置好", status_code=404)
        return self.json(
            {
                "fingerprint": runtime.fingerprint,
                "source": runtime.source_label,
                "options": runtime.options,
                "styles": runtime.styles,
            }
        )


def _strip_suffix(value: str) -> str:
    return str(value).split(".", 1)[0]


VIEW_CLASSES = (CnMapsModuleView, CnMapsTileView, CnMapsStyleView)

__all__ = [
    "CnMapsModuleView",
    "CnMapsStyleView",
    "CnMapsTileView",
    "VIEW_CLASSES",
]
