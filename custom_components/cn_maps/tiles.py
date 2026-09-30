"""服务端瓦片服务：抓上游瓦片、按需拼合、缓存。

合并了原来的 tiles.py（瓦片服务）和 compose.py（解码、缩放、裁切、合成、编码）。

为什么把瓦片放到服务端做：

* Home Assistant 内置地图的瓦片请求由 MapLibre 的 worker / Leaflet 的 ``<img>``
  发出，拿不到鉴权头，浏览器侧也不该去操心 GCJ-02 偏移和密钥；
* 密钥只留在服务端，浏览器拿不到，也不受 Referer 限制；
* 纠偏在服务端算一次，所有客户端（含手机 App、无 WebGL2 的老设备）完全一致。

拼合规则见 projection.plan_tile：输出瓦片 (z,x,y) 对应 1~4 张（百度最多 3x3
张）上游瓦片，每张按 (dx, dy) 画在 256x256 画布上。只有「单张 + 1:1 + 无偏移」
时才直接透传上游字节，其余都要解码重编码。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
from io import BytesIO
from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import TILE_CACHE_SIZE, UPSTREAM_TIMEOUT
from .projection import TILE_SIZE, plan_targets, plan_tile
from .sources import MapSource

_LOGGER = logging.getLogger(__name__)

_MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
_MAX_CONCURRENT_UPSTREAM = 8
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ------------------------------------------------------- 图像编解码（原 compose）
try:  # Pillow 是 Home Assistant 核心依赖，正常一定存在
    from PIL import Image
except ImportError:  # pragma: no cover - 只在极端精简的安装里发生
    Image = None  # type: ignore[assignment]

PNG = "image/png"
JPEG = "image/jpeg"

#: Pillow 是否可用（不可用时集成会退化成「整张瓦片」模式）
PILLOW_AVAILABLE = Image is not None


def decode_tile(data: bytes) -> Any:
    """把上游瓦片字节解码成 RGBA 图；失败返回 None。"""
    if Image is None:
        return None
    try:
        image = Image.open(BytesIO(data))
        image.load()
    except Exception:  # noqa: BLE001 - 上游可能返回半张图 / HTML / JSON
        return None
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    if image.size != (TILE_SIZE, TILE_SIZE):
        image = image.resize((TILE_SIZE, TILE_SIZE), Image.LANCZOS)
    return image


def paste_tile(
    canvas: Any, image: Any, dx: float, dy: float, width: float, height: float
) -> None:
    """把 image 画到 canvas 的 (dx, dy) 处，缩放到 width×height，越界部分裁掉。"""
    draw_w = max(1, int(round(width)))
    draw_h = max(1, int(round(height)))
    x = int(dx)
    y = int(dy)
    left = max(0, x)
    top = max(0, y)
    right = min(TILE_SIZE, x + draw_w)
    bottom = min(TILE_SIZE, y + draw_h)
    if right <= left or bottom <= top:
        return
    if image.size != (draw_w, draw_h):
        resample = Image.LANCZOS if draw_w < image.size[0] else Image.BICUBIC
        image = image.resize((draw_w, draw_h), resample)
    crop = image.crop((left - x, top - y, right - x, bottom - y))
    canvas.alpha_composite(crop, (left, top))


def compose_layer(
    base: Any,
    items: list[tuple[tuple[bytes, str], float, float]],
    width: float,
    height: float,
) -> tuple[Any, set[str], int]:
    """拼一层瓦片，返回 (画布, 用到的 content-type 集合, 成功张数)。"""
    if Image is None:  # pragma: no cover
        raise RuntimeError("Pillow 不可用")
    canvas = base if base is not None else Image.new("RGBA", (TILE_SIZE, TILE_SIZE))
    content_types: set[str] = set()
    drawn = 0
    for (data, content_type), dx, dy in items:
        image = decode_tile(data)
        if image is None:
            continue
        paste_tile(canvas, image, dx, dy, width, height)
        content_types.add(content_type)
        drawn += 1
    return canvas, content_types, drawn


def is_fully_opaque(canvas: Any) -> bool:
    """整幅不透明（可以用 JPEG 编码，不用担心边缘发黑）。"""
    if Image is None:  # pragma: no cover
        return False
    return canvas.getchannel("A").getextrema()[0] == 255


def encode_canvas(canvas: Any, allow_jpeg: bool) -> tuple[bytes, str]:
    """编码输出：整幅不透明且底层全是 JPEG 时用 JPEG（体积小一个数量级）。"""
    buffer = BytesIO()
    if allow_jpeg:
        canvas.convert("RGB").save(buffer, format="JPEG", quality=88)
        return buffer.getvalue(), JPEG
    canvas.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue(), PNG


# ----------------------------------------------------------- 瓦片服务（原 tiles）
@dataclass(frozen=True, slots=True)
class TileSettings:
    """渲染一张瓦片需要的全部设置（变了就等于换了一套缓存）。"""

    fingerprint: str
    source: MapSource
    api_key: str = ""
    fix_offset: bool = True
    precise: bool = True
    debug: bool = False


class TileService:
    """瓦片抓取 + 拼合 + 内存缓存。"""

    def __init__(self, hass: HomeAssistant, cache_size: int = TILE_CACHE_SIZE) -> None:
        self._hass = hass
        self._session = async_get_clientsession(hass)
        self._cache: OrderedDict[tuple, tuple[bytes, str]] = OrderedDict()
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._cache_size = cache_size
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPSTREAM)
        self._warned: set[str] = set()

    # ------------------------------------------------------------ 对外接口
    async def async_get_tile(
        self, settings: TileSettings, z: int, x: int, y: int
    ) -> tuple[bytes, str] | None:
        """取一张合成好的瓦片；拿不到返回 None。"""
        if not (0 <= z <= 22) or x < 0 or y < 0 or x >= 2**z or y >= 2**z:
            return None

        key = (settings.fingerprint, z, x, y)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._render(settings, z, x, y))
            self._inflight[key] = task
            task.add_done_callback(lambda _t, k=key: self._inflight.pop(k, None))

        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - 单张瓦片出错不该影响地图其它部分
            _LOGGER.debug("cn_maps: 瓦片 %s/%s/%s 渲染失败: %s", z, x, y, err)
            return None

        if result is not None:
            self._cache[key] = result
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    # ------------------------------------------------------------ 内部实现
    async def _render(
        self, settings: TileSettings, z: int, x: int, y: int
    ) -> tuple[bytes, str] | None:
        source = settings.source
        plan = plan_tile(
            source,
            z,
            x,
            y,
            fix_offset=settings.fix_offset,
            precise=settings.precise,
        )

        # 最常见的好情况：一张、1:1、无偏移 —— 直接透传上游字节
        if plan.passthrough:
            targets = plan_targets(source, plan, settings.api_key, 0)
            if not targets:
                return None
            return await self._fetch_one(targets[0][0], settings)

        if not PILLOW_AVAILABLE:
            self._warn_once(
                "no_pillow",
                "cn_maps: 没找到 Pillow，无法做像素级纠偏，已退化为「整张瓦片」模式",
            )
            return await self._fetch_fallback(source, settings, z, x, y)

        canvas: Any = None
        used_types: set[str] = set()
        for layer in range(len(source.layers)):
            targets = plan_targets(source, plan, settings.api_key, layer)
            if not targets:
                continue
            fetched = await asyncio.gather(
                *(self._fetch_one(url, settings) for url, _dx, _dy in targets)
            )
            items = [
                (item, dx, dy)
                for (_url, dx, dy), item in zip(targets, fetched)
                if item is not None
            ]
            if not items:
                if layer == 0:
                    return None
                break
            canvas, content_types, drawn = await self._hass.async_add_executor_job(
                compose_layer, canvas, items, plan.tile_px_x, plan.tile_px_y
            )
            used_types |= content_types
            if layer == 0 and drawn == 0:
                return None

        if canvas is None:
            return None

        allow_jpeg = (
            bool(used_types)
            and all(content_type == JPEG for content_type in used_types)
            and is_fully_opaque(canvas)
        )
        return await self._hass.async_add_executor_job(encode_canvas, canvas, allow_jpeg)

    async def _fetch_fallback(
        self, source: MapSource, settings: TileSettings, z: int, x: int, y: int
    ) -> tuple[bytes, str] | None:
        """没有 Pillow 时的保底：只取一张最接近的瓦片。"""
        plan = plan_tile(source, z, x, y, fix_offset=settings.fix_offset, precise=False)
        targets = plan_targets(source, plan, settings.api_key, 0)
        if not targets:
            return None
        return await self._fetch_one(targets[0][0], settings)

    async def _fetch_one(
        self, url: str, settings: TileSettings
    ) -> tuple[bytes, str] | None:
        headers = {
            "User-Agent": _UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        referer = settings.source.referer
        if referer:
            headers["Referer"] = referer

        try:
            async with self._semaphore:
                async with self._session.get(
                    url, headers=headers, timeout=ClientTimeout(total=UPSTREAM_TIMEOUT)
                ) as resp:
                    if resp.status != 200:
                        if settings.debug:
                            _LOGGER.warning("cn_maps: 上游返回 %s：%s", resp.status, url)
                        return None
                    content_type = (
                        resp.headers.get("Content-Type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    data = await resp.read()
        except (ClientError, asyncio.TimeoutError) as err:
            if settings.debug:
                _LOGGER.warning("cn_maps: 上游请求失败 %s：%s", url, err)
            return None

        if not data or len(data) > _MAX_UPSTREAM_BYTES:
            return None
        if not content_type.startswith("image/"):
            if settings.debug:
                _LOGGER.warning(
                    "cn_maps: 上游返回的不是图片（%s）：%s", content_type, url
                )
            return None
        return (data, content_type)

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        _LOGGER.warning(message)


__all__ = [
    "JPEG",
    "PILLOW_AVAILABLE",
    "PNG",
    "TileService",
    "TileSettings",
    "compose_layer",
    "decode_tile",
    "encode_canvas",
    "is_fully_opaque",
    "paste_tile",
]
