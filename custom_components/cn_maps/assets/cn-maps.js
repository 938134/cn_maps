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

  // 版本号只维护 manifest.json 一份：发布流程从 tag 同步 manifest，
  // 后端（runtime.build_frontend_config）再把版本注入到 window.__cnMapsConfig。
  // 这里直接读注入值，避免两处手工维护导致漂移。
  var INJECTED = (typeof window !== "undefined" && window.__cnMapsConfig) || {};

  var VERSION = String(INJECTED.version || "0");

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
   * 在官方地图的「切换分组 / 重置焦点」按钮列尾部追加一个「切换底图」按钮。
   *
   * 那列按钮是 hui-map-card / ha-map 渲染的 <div id="buttons">，里面是若干
   * <ha-icon-button>（Lit 模板，非 Leaflet 控件）。往同一容器 append 一个
   * 同款 ha-icon-button，样式、尺寸、排列自然与官方按钮一致。
   */
  function injectMapButton() {
    if (!CONFIG.sources || CONFIG.sources.length === 0) return;

    var hass = null;
    var observer = null;

    // 官方按钮容器：hui-map-card 和 ha-map（区域编辑器等）都用 id="buttons"
    var BTN_CONTAINERS = '#buttons, .map-buttons';

    function deepQueryAll(root, selector) {
      var results = [];
      if (!root) return results;
      // 先查当前层
      if (root.querySelectorAll) {
        root.querySelectorAll(selector).forEach(function (el) {
          results.push(el);
        });
        // 递归 shadow DOM
        root.querySelectorAll("*").forEach(function (el) {
          if (el.shadowRoot) {
            deepQueryAll(el.shadowRoot, selector).forEach(function (r) {
              results.push(r);
            });
          }
        });
      }
      return results;
    }

    /** 往单个按钮容器注入切换按钮（幂等，已注入则跳过） */
    function injectIntoContainer(container) {
      if (container.querySelector(".cn-maps-switch-btn")) return; // 已注入

      // 统一按钮配色：官方「切换分组/重置焦点」与本按钮都用实底背景，
      // 观感与放大缩小按钮一致；颜色用 HA 主题变量，自动跟随亮暗模式
      var rootNode = container.getRootNode();
      if (rootNode && !rootNode.querySelector("#cn-maps-btn-style")) {
        var styleEl = document.createElement("style");
        styleEl.id = "cn-maps-btn-style";
        styleEl.textContent =
          "#buttons ha-icon-button{background-color:var(--card-background-color,#fff);" +
          "color:var(--primary-text-color,#212121);border-radius:50%;" +
          "box-shadow:0 0 0 2px rgba(0,0,0,.1)}" +
          "#buttons ha-icon-button:hover{filter:brightness(.92)}";
        (rootNode === document ? document.head : rootNode).appendChild(styleEl);
      }

      // 创建与官方按钮同款的 ha-icon-button
      var btn = document.createElement("ha-icon-button");
      btn.className = "cn-maps-switch-btn";
      btn.setAttribute("label", "切换底图");
      btn.title = "切换底图：" + (CONFIG.sourceLabel || CONFIG.source || "");
      btn.tabIndex = 0;
      // 用与官方一致的 Material Design 图标（layers 图标，语义即「图层/底图」）
      btn.innerHTML = '<svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor" style="pointer-events:none">' +
        '<path d="M11.99 18.54l-7.37-5.73L3 14.07l9 7 9-7-1.63-1.27-7.38 5.74zM12 16l7.36-5.73L21 9l-9-7-9 7 1.63 1.27L12 16z"/>' +
        '</svg>';

      btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        showSourceMenu(btn);
      });

      container.appendChild(btn);
      if (CONFIG.debug) console.info("[cn_maps] 切换按钮已注入官方按钮列");
    }

    /** 在某个地图元素（hui-map-card / ha-map）的渲染根里找容器注入 */
    function injectIntoHost(host) {
      var root = host.renderRoot || host.shadowRoot;
      if (!root || !root.querySelectorAll) return;
      try {
        root.querySelectorAll(BTN_CONTAINERS).forEach(injectIntoContainer);
      } catch (e) {}
    }

    /** 全文档兜底扫描（含 shadow DOM） */
    function tryInject() {
      var containers = deepQueryAll(document, BTN_CONTAINERS);
      containers.forEach(injectIntoContainer);
      return containers.length > 0;
    }

    /** 从后端取最新配置（style.json 需要鉴权，带上 HA 的 access token） */
    function fetchNewConfig() {
      var headers = {};
      try {
        if (hass && hass.auth && hass.auth.data) {
          headers.Authorization = "Bearer " + hass.auth.data.access_token;
        }
      } catch (e) {}
      return fetch("/cn_maps/style.json", { headers: headers }).then(function (r) {
        return r.ok ? r.json() : null;
      });
    }

    /** 免刷新热切换：更新配置 + 让地图重载样式 */
    function applyNewConfig(cfg) {
      var tileTemplate = "";
      try {
        tileTemplate = cfg.styles.light.sources["cn-base"].tiles[0];
      } catch (e) {}
      if (!tileTemplate) return false;

      var label = cfg.source || "";
      (CONFIG.sources || []).forEach(function (s) {
        if (s.key === cfg.source) label = s.label;
      });

      applyConfig({
        fingerprint: cfg.fingerprint,
        source: cfg.source,
        sourceLabel: label,
        tileTemplate: tileTemplate,
        styles: cfg.styles,
      });

      // 引擎没有公开的「重载样式」接口，只有 setDarkMode(布尔)。
      // 连续翻转两次（A→!A→A）：引擎内部的请求序号保证只有最后一次
      // 生效，而最后一次 loadStyle 走我们的 fetch 钩子拿到新样式，
      // 视觉上无闪烁、实体图层也不会丢。
      var ok = false;
      deepQueryAll(document, "ha-map").forEach(function (m) {
        try {
          var engine = m._engine;
          if (engine && typeof engine.setDarkMode === "function") {
            var cur = engine._requestedDarkMode;
            if (cur === undefined && hass && hass.themes) {
              cur = !!hass.themes.darkMode;
            }
            engine.setDarkMode(!cur);
            engine.setDarkMode(!!cur);
            ok = true;
          }
        } catch (e) {
          if (CONFIG.debug) console.warn("[cn_maps] 热切换地图样式失败：", e);
        }
      });
      return ok;
    }

    /** 切换数据源：调服务 → 轮询新配置 → 热切换；失败才整页刷新 */
    function switchSource(srcKey, onDone) {
      if (!hass) hass = getHass();
      if (!hass) {
        location.reload();
        return;
      }
      try {
        hass.callService("cn_maps", "set_source", { source: srcKey });
      } catch (e) {
        if (CONFIG.debug) console.warn("[cn_maps] callService 异常：", e);
      }

      var attempts = 0;
      function poll() {
        attempts++;
        fetchNewConfig()
          .then(function (cfg) {
            if (cfg && cfg.fingerprint && cfg.fingerprint !== CONFIG.fingerprint) {
              if (applyNewConfig(cfg)) {
                if (CONFIG.debug) console.info("[cn_maps] 已热切换到 " + cfg.source);
                if (onDone) onDone();
                return;
              }
              location.reload(); // 热切换不可用，退回整页刷新
              return;
            }
            if (attempts < 6) {
              setTimeout(poll, 500);
            } else {
              location.reload(); // 后端迟迟没更新，退回整页刷新
            }
          })
          .catch(function () {
            if (attempts < 6) setTimeout(poll, 500);
            else location.reload();
          });
      }
      setTimeout(poll, 300);
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
          if (src.key === CONFIG.source) {
            menu.remove();
            return;
          }
          item.textContent = "切换中…";
          switchSource(src.key, function () {
            menu.remove();
          });
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

    // ---- 首选：挂钩地图元素的渲染生命周期 ----
    // hui-map-card / ha-map 是 Lit 组件，每次（重）渲染都会走
    // firstUpdated() / updated() 生命周期。把注入逻辑挂上去：卡片创建、
    // 切菜单回来重建、样式更新重渲染的瞬间就地注入——时机精确、零轮询。
    // 组件可能是懒加载（首次打开地图才 define），因此同时拦截
    // customElements.define，定义时自动补挂钩。
    var TARGET_ELEMENTS = ["hui-map-card", "ha-map"];
    var patchedCtors = {};

    function patchMapElement(name) {
      if (patchedCtors[name]) return true;
      var registry = window.customElements;
      var ctor = registry && registry.get ? registry.get(name) : null;
      if (!ctor || !ctor.prototype) return false;
      patchedCtors[name] = true;
      var proto = ctor.prototype;
      ["firstUpdated", "updated"].forEach(function (method) {
        var orig = proto[method];
        if (typeof orig !== "function" || orig.__cnMapsWrapped) return;
        var wrapped = function () {
          var result = orig.apply(this, arguments);
          try { injectIntoHost(this); } catch (e) {}
          return result;
        };
        wrapped.__cnMapsWrapped = true;
        proto[method] = wrapped;
      });
      if (CONFIG.debug) console.info("[cn_maps] 已挂钩 " + name + " 渲染生命周期");
      return true;
    }

    function patchElementRegistry() {
      if (typeof CustomElementRegistry === "undefined") return;
      var proto = CustomElementRegistry.prototype;
      if (proto.__cnMapsPatched) return;
      proto.__cnMapsPatched = true;
      var origDefine = proto.define;
      proto.define = function (name, ctor, opts) {
        var result = origDefine.apply(this, arguments);
        if (TARGET_ELEMENTS.indexOf(name) !== -1) patchMapElement(name);
        return result;
      };
    }

    TARGET_ELEMENTS.forEach(patchMapElement); // 已定义的直接挂
    patchElementRegistry();                   // 未定义的等 define 时挂
    tryInject();                              // 兼容已渲染完成的现存实例

    // ---- 兜底：DOM 监听（仅当生命周期挂钩失效时起作用）----
    // 若未来 HA 改了组件名，挂钩会静默失效，用 MutationObserver（递归
    // 覆盖 shadow DOM，HA 界面全在 shadow DOM 里渲染）+ 路由事件 +
    // 定时扫描兜底，保证按钮最终仍会出现（注入有幂等保护，不重复）。
    var observedRoots = typeof WeakSet !== "undefined" ? new WeakSet() : null;
    var pendingInject = false;

    function attachObserver(root) {
      if (!observedRoots || !root) return;
      if (observedRoots.has(root)) return;
      observedRoots.add(root);
      observer.observe(root, { childList: true, subtree: true });
    }

    function observeShadowRoots(root) {
      if (!root || !root.querySelectorAll) return;
      attachObserver(root);
      var all = root.querySelectorAll("*");
      for (var i = 0; i < all.length; i++) {
        if (all[i].shadowRoot) observeShadowRoots(all[i].shadowRoot);
      }
    }

    function scheduleInject() {
      if (pendingInject) return;
      pendingInject = true;
      setTimeout(function () {
        pendingInject = false;
        observeShadowRoots(document); // 顺带给新出现的 shadow root 补挂 observer
        tryInject();
      }, 150);
    }

    observer = new MutationObserver(scheduleInject);
    observeShadowRoots(document);
    window.addEventListener("location-changed", scheduleInject);
    setInterval(scheduleInject, 2000);

    if (CONFIG.debug) console.info("[cn_maps] 生命周期挂钩 + DOM 兜底监听已就绪");
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
