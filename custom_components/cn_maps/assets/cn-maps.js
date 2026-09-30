/**
 * cn_maps —— 把 Home Assistant 内置地图的底图换成国内地图源。
 *
 * 这是集成注入的「胶水」脚本，它做三件事：
 *   1. 把 HA 内置的 /static/map/light.json、/static/map/dark.json 换成集成生成的样式；
 *   2. 在没有 WebGL2 的设备上，把内置的栅格瓦片地址换成同一套接口；
 *   3. 在地图自带的控件组里注入「切换底图」按钮，跟放大缩小按钮在一起。
 */
(function () {
  "use strict";

  var VERSION = "2026.09.30";

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

  var stats = { styleHits: 0, rasterTiles: 0, cartoTiles: 0, errors: [] };
  window.__cnMapsStats = stats;

  if (!CONFIG.styles || !CONFIG.styles.light) {
    console.warn("[cn_maps] 没有拿到集成注入的配置，脚本不生效。");
    window.cnMaps = { version: VERSION, active: false, stats: stats };
    return;
  }

  /* ------------------------------------------------------------------ 工具 */

  function instanceOrigin() {
    if (typeof location !== "undefined" && location.origin) return location.origin;
    return "";
  }

  function absolutize(template) {
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(template)) return template;
    return instanceOrigin() + (template.charAt(0) === "/" ? "" : "/") + template;
  }

  function tileUrl(z, x, y) {
    return CONFIG.tileTemplate.replace("{z}", z).replace("{x}", x).replace("{y}", y);
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
    if (!preparedStyles[name]) preparedStyles[name] = prepareStyle(name);
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

  var nativeFetch = typeof window !== "undefined" && window.fetch ? window.fetch.bind(window) : null;

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

  var imgProto = typeof HTMLImageElement !== "undefined" ? HTMLImageElement.prototype : null;
  var srcDescriptor = imgProto ? Object.getOwnPropertyDescriptor(imgProto, "src") : null;

  if (srcDescriptor && srcDescriptor.set && srcDescriptor.get) {
    Object.defineProperty(imgProto, "src", {
      configurable: true,
      enumerable: srcDescriptor.enumerable,
      get: function () { return srcDescriptor.get.call(this); },
      set: function (value) {
        var next = rewriteTileUrl(value);
        srcDescriptor.set.call(this, next || value);
      },
    });
  }

  /* ------------------------------------------------------- 3. 注入地图控件 */

  function getHass() {
    var el = document.querySelector("home-assistant");
    if (el && el.hass) return el.hass;
    if (el && el.shadowRoot) {
      var inner = el.shadowRoot.querySelector("home-assistant-main") || el.shadowRoot.querySelector("ha-panel");
      if (inner && inner.hass) return inner.hass;
    }
    return null;
  }

  /**
   * 用 MutationObserver 监听地图控件出现，把「切换底图」按钮注入到控件组里。
   * MapLibre 的控件组是 .maplibregl-ctrl-group，Leaflet 是 .leaflet-control-zoom。
   * 这样按钮自然跟着地图走：地图在就在，地图不在就不在。
   */
  function injectMapButton() {
    if (!CONFIG.sources || CONFIG.sources.length === 0) return;

    var hass = null;
    var observer = null;

    // 查找地图控件组的 CSS 选择器
    var CTRL_SELECTORS = ".maplibregl-ctrl-group, .leaflet-control-zoom";

    function findControlGroup() {
      // 先在 document 里找
      var el = document.querySelector(CTRL_SELECTORS);
      if (el) return el;
      // 再在 shadow DOM 里找
      var ha = document.querySelector("home-assistant");
      if (ha && ha.shadowRoot) {
        el = ha.shadowRoot.querySelector(CTRL_SELECTORS);
        if (el) return el;
        var main = ha.shadowRoot.querySelector("home-assistant-main");
        if (main && main.shadowRoot) {
          el = main.shadowRoot.querySelector(CTRL_SELECTORS);
          if (el) return el;
          // Lovelace 卡片可能在更深处
          var panel = main.shadowRoot.querySelector("ha-panel-lovelace");
          if (panel && panel.shadowRoot) {
            el = panel.shadowRoot.querySelector(CTRL_SELECTORS);
            if (el) return el;
          }
        }
      }
      return null;
    }

    function deepQueryAll(root, selector) {
      var results = [];
      if (!root) return results;
      // 先查当前层
      root.querySelectorAll && root.querySelectorAll(selector).forEach(function (el) {
        results.push(el);
      });
      // 递归 shadow DOM
      root.querySelectorAll && root.querySelectorAll("*").forEach(function (el) {
        if (el.shadowRoot) {
          deepQueryAll(el.shadowRoot, selector).forEach(function (r) {
            results.push(r);
          });
        }
      });
      return results;
    }

    function tryInject() {
      // 深度查找所有地图控件组
      var groups = deepQueryAll(document, CTRL_SELECTORS);
      if (groups.length === 0) return false;

      var injected = false;
      groups.forEach(function (group) {
        if (group.querySelector(".cn-maps-switch-btn")) return; // 已注入

        // 创建切换按钮，完全继承 MapLibre/Leaflet 控件组按钮样式
        var btn = document.createElement("button");
        btn.className = "cn-maps-switch-btn maplibregl-ctrl-icon";
        btn.type = "button";
        btn.setAttribute("aria-label", "切换底图");
        btn.title = "切换底图：" + (CONFIG.sourceLabel || CONFIG.source || "");
        // 用内联 SVG 做图标，尺寸跟 MapLibre 默认图标一致（20x20）
        btn.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" style="display:block;margin:auto">' +
          '<path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 110-5 2.5 2.5 0 010 5z"/>' +
          '</svg>';
        // 不加任何 inline style，完全依赖同组按钮的 CSS

        btn.addEventListener("click", function (e) {
          e.preventDefault();
          e.stopPropagation();
          showSourceMenu(btn);
        });

        group.appendChild(btn);
        injected = true;
        if (CONFIG.debug) console.info("[cn_maps] 切换按钮已注入到地图控件组");
      });
      return injected;
    }

    function showSourceMenu(btnEl) {
      // 移除已有菜单
      var existing = document.getElementById("cn-maps-source-menu");
      if (existing) { existing.remove(); return; }

      if (!hass) hass = getHass();
      if (!hass) return;

      var menu = document.createElement("div");
      menu.id = "cn-maps-source-menu";
      menu.style.cssText = [
        "position: fixed",
        "z-index: 99999",
        "background: var(--card-background-color, #fff)",
        "border: 1px solid var(--divider-color, #ddd)",
        "border-radius: 8px",
        "box-shadow: 0 2px 12px rgba(0,0,0,0.2)",
        "padding: 4px",
        "font-family: var(--primary-font-family, sans-serif)",
        "font-size: 13px",
        "max-height: 320px",
        "overflow-y: auto",
      ].join(";");

      // 定位到按钮正下方
      var rect = btnEl.getBoundingClientRect();
      menu.style.left = rect.left + "px";
      menu.style.top = (rect.bottom + 4) + "px";

      CONFIG.sources.forEach(function (src) {
        var item = document.createElement("div");
        item.textContent = src.label;
        var isActive = src.key === CONFIG.source;
        item.style.cssText = [
          "padding: 8px 16px",
          "border-radius: 6px",
          "cursor: pointer",
          "white-space: nowrap",
          "background: " + (isActive ? "var(--primary-color, #03a9f4)" : "transparent"),
          "color: " + (isActive ? "var(--text-primary-color, #fff)" : "var(--primary-text-color, #333)"),
          "font-weight: " + (isActive ? "600" : "400"),
        ].join(";");

        item.addEventListener("mouseenter", function () {
          if (src.key !== CONFIG.source) {
            item.style.background = "var(--secondary-background-color, #f0f0f0)";
          }
        });
        item.addEventListener("mouseleave", function () {
          if (src.key !== CONFIG.source) {
            item.style.background = "transparent";
          }
        });
        item.addEventListener("click", function () {
          menu.remove();
          if (src.key === CONFIG.source) return;
          try {
            hass.callService("cn_maps", "set_source", { source: src.key });
          } catch (e) {
            if (CONFIG.debug) console.warn("[cn_maps] callService 异常：", e);
          }
          setTimeout(function () { location.reload(); }, 300);
        });

        menu.appendChild(item);
      });

      document.body.appendChild(menu);

      // 点击外部关闭菜单
      setTimeout(function () {
        function close(e) {
          if (!menu.contains(e.target)) {
            menu.remove();
            document.removeEventListener("click", close);
          }
        }
        document.addEventListener("click", close);
      }, 10);
    }

    // 用 MutationObserver 监听 DOM 变化，地图控件一出现就注入
    observer = new MutationObserver(function () {
      tryInject();
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // 立即试一次（地图可能已经渲染了）
    setTimeout(function () { tryInject(); }, 500);

    if (CONFIG.debug) console.info("[cn_maps] 开始监听地图控件，准备注入切换按钮");
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

  // 注入地图切换按钮
  injectMapButton();

  if (shouldReload) {
    Promise.resolve().then(function () {
      try {
        var key = "__cnMapsReloadedFor";
        if (sessionStorage.getItem(key) === nextVersion) return;
        sessionStorage.setItem(key, nextVersion);
      } catch (err) {}
      location.reload();
    });
  }
})();
