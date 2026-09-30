"""cn_maps 的常量定义。

集成只做一件事：把 Home Assistant 内置地图（MapLibre / Leaflet）的底图换成
国内地图源。瓦片抓取、坐标换算、密钥使用全部在 HA 服务端完成，浏览器端只
换一个地图样式。
"""

from __future__ import annotations

DOMAIN = "cn_maps"
NAME = "中国地图"

# ---------------------------------------------------------------- 选项键名
CONF_MAP_SOURCE = "map_source"
CONF_API_KEY = "api_key"  # 只存在于表单里：当前选中数据源的 Key
CONF_KEYS = "keys"  # 真正落盘的：{密钥名: 密钥}
CONF_FIX_CHINA_OFFSET = "fix_china_offset"
CONF_PRECISE_OFFSET = "precise_offset"
CONF_DARKEN_IN_DARK_MODE = "darken_in_dark_mode"
CONF_DEBUG = "debug"
CONF_AUTO_RELOAD = "auto_reload"

DEFAULT_OPTIONS: dict = {
    CONF_MAP_SOURCE: "",  # 空 = 用 sources.DEFAULT_SOURCE
    CONF_KEYS: {},
    CONF_FIX_CHINA_OFFSET: True,
    CONF_PRECISE_OFFSET: True,
    CONF_DARKEN_IN_DARK_MODE: False,
    CONF_DEBUG: False,
    CONF_AUTO_RELOAD: True,
}

# 选项流里的字段分组
BASIC_FIELDS = (CONF_MAP_SOURCE, CONF_FIX_CHINA_OFFSET, CONF_PRECISE_OFFSET)
ADVANCED_FIELDS = (
    CONF_DARKEN_IN_DARK_MODE,
    CONF_DEBUG,
    CONF_AUTO_RELOAD,
)

# ---------------------------------------------------------------- 前端资源
ASSET_DIR = "assets"
ASSET_FILE = "cn-maps.js"

VIEW_NAME = "cn_maps:frontend"
MODULE_PATH = f"/{DOMAIN}/frontend"
# 前端模块带指纹：内容一变 URL 就变，浏览器缓存自动失效
MODULE_URL_TEMPLATE = f"{MODULE_PATH}/{{fingerprint}}/{ASSET_FILE}"

TILES_PATH = f"/{DOMAIN}/tiles"
STYLE_PATH = f"/{DOMAIN}/style.json"

# ---------------------------------------------------------------- hass.data
DATA_RUNTIME = "runtime"
DATA_MODULE_URL = "module_url"
DATA_VIEWS_REGISTERED = "views_registered"

# 瓦片缓存条数（每条约 10~40 KB）
TILE_CACHE_SIZE = 512

# 上游请求超时（秒）
UPSTREAM_TIMEOUT = 15
