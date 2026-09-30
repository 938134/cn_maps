/**
 * cn_maps —— 把 Home Assistant 内置地图的底图换成国内地图源。
 *
 * 这是集成注入的「胶水」脚本，它做三件事：
 *
 *   1. 把 HA 内置的 /static/map/light.json、/static/map/dark.json 换成集成生成的
 *      样式（样式里的瓦片地址指向本集成在 HA 服务端的瓦片接口）；
 *   2. 在没有 WebGL2、HA 退回 Leaflet 光栅渲染的设备上，把内置的栅格瓦片地址
 *      （/api/map_tiles/raster/...，以及老版本 HA 的 CARTO 瓦片）换成同一套接口；
 *   3. 在页面右下角放一个浮动工具栏，可一键切换底图数据源。
 */
(function () {
  "use strict";

  var VERSION = "2026.09.30";

  /** 集成注入的配置（见 custom_components/cn_maps/runtime.py） */
  var CONFIG = {
    version: VERSION,
    fingerprint: "",
    source: "",
    sourceLabel: "",
    tileTemplate: "",
    debug: false,
    autoReload: true,
    styles: { light: null, dark: null },
    sources: [],
  };

  var INJECTED = (typeof window !== "undefined" && window.__cnMapsConfig) || {};

  function applyInjected(source, target) {
    Object.keys(target).forEach(function (key) {
      var value = source[key];
      if (value !== undefined && value !== null) {
        target[key] = value;
      }
    });
  }

  applyInjected(INJECTED, CONFIG);

  var stats = {
    styleHits: 0,
    rasterTiles: 0,
    cartoTiles: 0,
    errors: [],
  };

  window.__cnMapsStats = stats;

  if (!CONFIG.styles || !CONFIG.styles.light) {
    console.warn(
      "[cn_maps] 没有拿到集成注入的配置，脚本不生效。"
    );
    window.cnMaps = { version: VERSION, active: false, stats: stats };
    return;
  }

  /* ------------------------------------------------------------------ 工具 */

  function instanceOrigin() {
    if (typeof location !== "undefined" && location.origin) {
      return location.origin;
    }
    return "";
  }

  function absolutize(template) {
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(template)) {
      return template;
    }
    return instanceOrigin() + (template.charAt(0) === "/" ? "" : "/") + template;
  }

  function tileUrl(z, x, y) {
    return CONFIG.tileTemplate
      .replace("{z}", z)
      .replace("{x}", x)
      .replace("{y}", y);
  }

  /* ------------------------------------------------------- 1. 换地图样式 */

  var STYLE_RE = /\/static\/map\/(light|dark)\.json$/;
  var preparedStyles = { light: null, dark: null };

  function prepareStyle(name) {
    var raw = CONFIG.styles[name];
    if (!raw) return null;
    var sources = {};
    Object.keys(raw.sources || {}).forEach(function (id) {
      var source = raw.sources[id];
      sources[id] = Array.isArray(source.tiles)
        ? Object.assign({}, source, { tiles: source.tiles.map(absolutize) })
        : Object.assign({}, source);
    });
    return Object.assign({}, raw, { sources: sources });
  }

  function styleFor(url) {
    if (!url || url.indexOf("static/map/") === -1) return null;
    var match = STYLE_RE.exec(url);
    if (!match) return null;
    var name = match[1];
    if (!preparedStyles[name]) {
      preparedStyles[name] = prepareStyle(name);
    }
    return preparedStyles[name];
  }

  /* ------------------------------------ 2. 兜底：无 WebGL2 时的栅格瓦片 */

  var RASTER_RE = /\/api\/map_tiles\/raster\/(\d+)\/(\d+)\/(\d+)\.png(?:[?#].*)?$/;
  var CARTO_RE = /cartodb-basemaps-[a-z]\.global\.ssl\.fastly\.net\/[^?#]*\/(\d+)\/(\d+)\/(\d+)\.png(?:[?#].*)?$/;

  function rewriteTileUrl(raw) {
    if (!raw || typeof raw !== "string" || !CONFIG.tileTemplate) return null;
    var match = RASTER_RE.exec(raw);
    if (match) {
      stats.rasterTiles++;
      return absolutize(tileUrl(match[1], match[2], match[3]));
    }
    match = CARTO_RE.exec(raw);
    if (match) {
      stats.cartoTiles++;
      return absolutize(tileUrl(match[1], match[2], match[3]));
    }
    return null;
  }

  /* ---------------------------------------------------------- 装钩子 */

  var nativeFetch =
    typeof window !== "undefined" && window.fetch
      ? window.fetch.bind(window)
      : null;

  function requestUrl(input) {
    if (typeof input === "string") return input;
    if (input && typeof input.url === "string") return input.url;
    return "";
  }

  if (nativeFetch) {
    window.fetch = function (input, init) {
      var url = requestUrl(input);
      if (url) {
        var style = styleFor(url);
        if (style) {
          stats.styleHits++;
          if (CONFIG.debug) {
            console.info("[cn_maps] 已接管地图样式：", url, "->", CONFIG.sourceLabel);
          }
          return Promise.resolve(
            new Response(JSON.stringify(style), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            })
          );
        }
      }
      return nativeFetch(input, init);
    };
  }

  var imgProto =
    typeof HTMLImageElement !== "undefined" ? HTMLImageElement.prototype : null;
  var srcDescriptor = imgProto
    ? Object.getOwnPropertyDescriptor(imgProto, "src")
    : null;

  if (srcDescriptor && srcDescriptor.set && srcDescriptor.get) {
    Object.defineProperty(imgProto, "src", {
      configurable: true,
      enumerable: srcDescriptor.enumerable,
      get: function () {
        return srcDescriptor.get.call(this);
      },
      set: function (value) {
        var next = rewriteTileUrl(value);
        srcDescriptor.set.call(this, next || value);
      },
    });
  }

  /* ------------------------------------------------------- 3. 工具栏 */

  /** 从 HA 前端获取 hass 对象（用于调用服务） */
  function getHass() {
    var el = document.querySelector("home-assistant");
    if (el && el.hass) return el.hass;
    // 兼容：尝试 shadow DOM 里的子元素
    if (el && el.shadowRoot) {
      var inner = el.shadowRoot.querySelector("home-assistant-main") ||
                   el.shadowRoot.querySelector("ha-panel");
      if (inner && inner.hass) return inner.hass;
    }
    return null;
  }

  /** 在右下角放一个浮动工具栏，始终显示 */
  function createToolbar() {
    if (!CONFIG.sources || CONFIG.sources.length === 0) return;

    function tryCreate(retries) {
      if (retries <= 0) return;
      if (!document.body) {
        setTimeout(function () { tryCreate(retries - 1); }, 500);
        return;
      }
      if (document.getElementById("cn-maps-toolbar")) return;

      var hass = getHass();
      if (!hass) {
        setTimeout(function () { tryCreate(retries - 1); }, 1000);
        return;
      }

      var toolbar = document.createElement("div");
      toolbar.id = "cn-maps-toolbar";
      toolbar.style.cssText = [
        "position: fixed",
        "right: 16px",
        "bottom: 48px",
        "z-index: 99999",
        "display: flex",
        "flex-direction: column",
        "align-items: flex-end",
        "gap: 6px",
        "font-family: var(--primary-font-family, sans-serif)",
        "font-size: 13px",
      ].join(";");

      // 收起/展开按钮
      var toggle = document.createElement("div");
      toggle.textContent = "\u{1F5FA}";
      toggle.title = "切换地图底图";
      toggle.style.cssText = [
        "width: 44px",
        "height: 44px",
        "border-radius: 50%",
        "background: var(--card-background-color, #fff)",
        "border: 1px solid var(--divider-color, #ddd)",
        "box-shadow: 0 2px 8px rgba(0,0,0,0.2)",
        "display: flex",
        "align-items: center",
        "justify-content: center",
        "cursor: pointer",
        "font-size: 22px",
        "user-select: none",
        "transition: transform 0.15s",
        "line-height: 1",
      ].join(";");

      toggle.addEventListener("mouseenter", function () {
        toggle.style.transform = "scale(1.1)";
      });
      toggle.addEventListener("mouseleave", function () {
        toggle.style.transform = "scale(1)";
      });
      toggle.addEventListener("click", function () {
        panel.style.display = panel.style.display === "none" ? "flex" : "none";
      });

      // 源列表面板
      var panel = document.createElement("div");
      panel.style.cssText = [
        "display: none",
        "flex-direction: column",
        "gap: 4px",
        "background: var(--card-background-color, #fff)",
        "border: 1px solid var(--divider-color, #ddd)",
        "border-radius: 8px",
        "box-shadow: 0 2px 12px rgba(0,0,0,0.2)",
        "padding: 6px",
        "max-height: 320px",
        "overflow-y: auto",
      ].join(";");

      CONFIG.sources.forEach(function (src) {
        var btn = document.createElement("div");
        btn.textContent = src.label;
        var isActive = src.key === CONFIG.source;
        btn.style.cssText = [
          "padding: 8px 16px",
          "border-radius: 6px",
          "cursor: pointer",
          "white-space: nowrap",
          "background: " + (isActive
            ? "var(--primary-color, #03a9f4)"
            : "transparent"),
          "color: " + (isActive
            ? "var(--text-primary-color, #fff)"
            : "var(--primary-text-color, #333)"),
          "font-weight: " + (isActive ? "600" : "400"),
          "transition: background 0.15s",
        ].join(";");

        btn.addEventListener("mouseenter", function () {
          if (src.key !== CONFIG.source) {
            btn.style.background = "var(--secondary-background-color, #f0f0f0)";
          }
        });
        btn.addEventListener("mouseleave", function () {
          if (src.key !== CONFIG.source) {
            btn.style.background = "transparent";
          }
        });
        btn.addEventListener("click", function () {
          if (src.key === CONFIG.source) {
            panel.style.display = "none";
            return;
          }
          // 调用 HA 服务切换底图
          btn.textContent = "切换中…";
          try {
            hass.callService("cn_maps", "set_source", { source: src.key });
          } catch (e) {
            if (CONFIG.debug) console.warn("[cn_maps] callService 异常：", e);
          }
          // callService 可能不返回 Promise，用短延迟等后端处理完再刷新
          setTimeout(function () {
            location.reload();
          }, 300);
        });

        panel.appendChild(btn);
      });

      toolbar.appendChild(panel);
      toolbar.appendChild(toggle);
      document.body.appendChild(toolbar);

      if (CONFIG.debug) {
        console.info("[cn_maps] 工具栏已创建，数据源：" + CONFIG.sources.length + " 个");
      }
    }

    // 多试几次，等 HA 前端初始化完成
    tryCreate(30);
  }

  /* ------------------------------------------- 设置变更后自动刷新一次 */

  var previousVersion = window.__cnMapsLoadedVersion;
  var nextVersion = CONFIG.fingerprint;
  window.__cnMapsLoadedVersion = nextVersion;

  var shouldReload =
    CONFIG.autoReload !== false &&
    !!previousVersion &&
    !!nextVersion &&
    previousVersion !== nextVersion;

  function applyConfig(next) {
    if (next) {
      applyInjected(next, CONFIG);
      preparedStyles.light = null;
      preparedStyles.dark = null;
    }
    return CONFIG;
  }

  window.__cnMaps = window.cnMaps = {
    version: VERSION,
    config: CONFIG,
    stats: stats,
    applyConfig: applyConfig,
    tileUrl: tileUrl,
    absolutize: absolutize,
  };

  console.info(
    "[cn_maps] v" + VERSION +
    " 已加载：底图=" + (CONFIG.sourceLabel || CONFIG.source || "?")
  );

  // 创建工具栏
  createToolbar();

  if (shouldReload) {
    Promise.resolve().then(function () {
      try {
        var key = "__cnMapsReloadedFor";
        if (sessionStorage.getItem(key) === nextVersion) return;
        sessionStorage.setItem(key, nextVersion);
      } catch (err) {}
      if (CONFIG.debug) {
        console.info("[cn_maps] 配置已更新，刷新页面：", previousVersion, "->", nextVersion);
      }
      location.reload();
    });
  }
})();
