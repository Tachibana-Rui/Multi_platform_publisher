/* ============================================================
 * 反自动化检测 + 硬件指纹扰动 init script
 * 通过 Playwright 的 context.add_init_script 在页面脚本之前注入
 *
 * 调用约定：Python 侧会在本脚本之前注入 window.__fp_cfg__，
 *     格式见 fingerprint_config.py / DEVICE_PRESETS
 *     若 window.__fp_cfg__ 不存在，则使用内置默认值。
 *
 * 注意：这里不要使用 ES6+ 语法（const / 箭头函数 / 模板字符串），
 *       保持 ES5 兼容以确保在旧内核上也能运行。
 * ============================================================ */

(function () {
  // --------------------------------------------------------------
  // 工具函数
  // --------------------------------------------------------------
  function _defineRO(obj, key, value) {
    try {
      Object.defineProperty(obj, key, {
        configurable: true,
        enumerable: true,
        writable: false,
        value: value,
      });
    } catch (e) {
      try { obj[key] = value; } catch (ee) {}
    }
  }

  function _defineGetter(obj, key, getter) {
    try {
      Object.defineProperty(obj, key, {
        configurable: true,
        enumerable: true,
        get: getter,
      });
    } catch (e) {
      try { obj[key] = getter(); } catch (ee) {}
    }
  }

  // 基于 seed 的可重复轻量 PRNG（同 JS 版 Mulberry32）
  function _makeRng(seed) {
    var t = (seed | 0) >>> 0;
    if (t === 0) t = 1;
    return function () {
      t = (t + 0x6D2B79F5) >>> 0;
      var x = t;
      x = Math.imul(x ^ (x >>> 15), x | 1);
      x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
      return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
    };
  }

  // --------------------------------------------------------------
  // 读取指纹配置；缺失则用默认桌面配置
  // --------------------------------------------------------------
  var DEFAULT_CFG = {
    profile: "desktop_default",
    platform: "Win32",
    vendor: "Google Inc.",
    screen_width: 1920,
    screen_height: 1080,
    avail_width: 1920,
    avail_height: 1040,
    outer_width: 1536,
    outer_height: 824,
    inner_width: 1519,
    inner_height: 746,
    screen_x: 0,
    screen_y: 0,
    device_pixel_ratio: 1,
    color_depth: 24,
    pixel_depth: 24,
    hardware_concurrency: 8,
    device_memory: 8,
    max_touch_points: 0,
    ontouchstart_present: false,
    timezone: "Asia/Shanghai",
    languages: ["zh-CN", "zh", "en-US", "en"],
    language: "zh-CN",
    accept_languages: "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    locale_country_code: "CN",
    webgl_vendor: "Google Inc. (NVIDIA)",
    webgl_renderer: "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-23.21.13.8792)",
    canvas_noise_seed: 12345,
    webgl_noise_seed: 67890,
    audio_noise_seed: 24680,
    fonts: ["Arial", "Microsoft YaHei", "微软雅黑", "Segoe UI", "Times New Roman", "SimSun"],
  };

  var cfg = (typeof window !== "undefined" && window.__fp_cfg__) ? window.__fp_cfg__ : DEFAULT_CFG;

  // --------------------------------------------------------------
  // 1) 清除 navigator.webdriver
  // --------------------------------------------------------------
  try {
    Object.defineProperty(navigator, "webdriver", {
      get: function () { return false; },
      configurable: true,
    });
  } catch (e) {}
  try { delete navigator.webdriver; } catch (e) {}

  // --------------------------------------------------------------
  // 2) 清除自动化工具注入的全局变量：
  //    window.cdc_*、window.__webdriver_*、__puppeteer_evaluation_script__ 等
  // --------------------------------------------------------------
  var blocklist = [
    /^cdc_/,
    /^__webdriver_/,
    /^\$cdc_/,
    /^__webdriverAsyncExecutor/,
    /^_Selenium_IDE_Recorder/,
    /^selenium/,
    /^__nightmare/,
    /^_phantom/,
    /^__puppeteer_evaluation_script__/,
  ];

  function _cleanAutomationKeys(obj) {
    if (!obj) return;
    var keys = [];
    try {
      for (var k in obj) keys.push(k);
    } catch (e) { return; }
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      for (var j = 0; j < blocklist.length; j++) {
        if (blocklist[j].test(key)) {
          try { obj[key] = undefined; delete obj[key]; } catch (e) {}
          break;
        }
      }
    }
  }

  _cleanAutomationKeys(window);
  if (typeof document !== "undefined") _cleanAutomationKeys(document);

  // --------------------------------------------------------------
  // 3) 补全 headless 模式缺失的 window.chrome 对象
  // --------------------------------------------------------------
  if (!window.chrome || !window.chrome.runtime) {
    var fakeChrome = {
      runtime: {
        OnInstalledReason: { INSTALL: "install", UPDATE: "update", CHROME_UPDATE: "chrome_update", SHARED_MODULE_UPDATE: "shared_module_update" },
        OnRestartRequiredReason: { APP_UPDATE: "app_update", OS_UPDATE: "os_update", PERIODIC: "periodic" },
        PlatformArch: { ARM: "arm", ARM64: "arm64", X86_32: "x86-32", X86_64: "x86-64" },
        PlatformNaclArch: { ARM: "arm", X86_32: "x86-32", X86_64: "x86-64" },
        PlatformOs: { ANDROID: "android", CROS: "cros", LINUX: "linux", MAC: "mac", WIN: "win" },
        RequestUpdateCheckStatus: { THROTTLED: "throttled", NO_UPDATE: "no_update", UPDATE_AVAILABLE: "update_available" },
        connect: function () {
          return {
            onDisconnect: { addListener: function () {} },
            onMessage: { addListener: function () {} },
            postMessage: function () {},
          };
        },
        sendMessage: function () {},
        getManifest: function () { return { manifest_version: 3, name: "__fake__", version: "1.0.0" }; },
        getURL: function (path) { return "chrome-extension://invalid/" + (path || ""); },
        id: "",
      },
      app: {
        isInstalled: false,
        getDetails: function (cb) { if (cb) cb({}); return Promise.resolve({}); },
        installState: { get: function (cb) { if (cb) cb({ isDisabled: false }); return Promise.resolve({ isDisabled: false }); } },
        runningState: { get: function (cb) { if (cb) cb({ state: "running" }); return Promise.resolve({ state: "running" }); } },
      },
      csi: function () {},
      loadTimes: function () {
        var now = Date.now() / 1000;
        return {
          requestTime: now,
          startLoadTime: now,
          commitLoadTime: now,
          finishDocumentLoadTime: now,
          finishLoadTime: now,
          firstPaintTime: now,
          firstPaintAfterLoadTime: now,
          wasFetchedViaSpdy: false,
          wasNpnNegotiated: false,
          npnNegotiatedProtocol: "h2",
          wasAlternateProtocolAvailable: false,
          connectionInfo: "http/1.1",
        };
      },
    };
    try {
      Object.defineProperty(window, "chrome", {
        value: fakeChrome,
        writable: true,
        configurable: true,
        enumerable: true,
      });
    } catch (e) {
      try { window.chrome = fakeChrome; } catch (ee) {}
    }
  }

  // --------------------------------------------------------------
  // 4) 屏幕特征：覆盖 screen.*、window.inner/outer、devicePixelRatio
  //    以及 colorDepth/pixelDepth
  // --------------------------------------------------------------
  function _applyScreen() {
    if (typeof screen === "undefined") return;

    var w = cfg.screen_width || 1920;
    var h = cfg.screen_height || 1080;
    var aw = cfg.avail_width || w;
    var ah = cfg.avail_height || h;

    _defineGetter(screen, "width", function () { return w; });
    _defineGetter(screen, "height", function () { return h; });
    _defineGetter(screen, "availWidth", function () { return aw; });
    _defineGetter(screen, "availHeight", function () { return ah; });
    _defineGetter(screen, "availLeft", function () { return cfg.screen_x || 0; });
    _defineGetter(screen, "availTop", function () { return cfg.screen_y || 0; });
    _defineGetter(screen, "colorDepth", function () { return cfg.color_depth || 24; });
    _defineGetter(screen, "pixelDepth", function () { return cfg.pixel_depth || 24; });
  }
  _applyScreen();

  // devicePixelRatio
  if (cfg.device_pixel_ratio && cfg.device_pixel_ratio !== window.devicePixelRatio) {
    try {
      Object.defineProperty(window, "devicePixelRatio", {
        configurable: true,
        get: function () { return cfg.device_pixel_ratio; },
      });
    } catch (e) {}
  }

  // outer/inner 尺寸——只在 headless / 非用户可交互窗口下才强行覆盖，
  // 真实窗口打开时让浏览器按实际尺寸返回更自然。
  function _applyWindowSize() {
    var ow = cfg.outer_width || cfg.screen_width;
    var oh = cfg.outer_height || cfg.screen_height;
    var iw = cfg.inner_width || (ow - 17);
    var ih = cfg.inner_height || (oh - 78);
    try {
      Object.defineProperty(window, "outerWidth", { configurable: true, get: function () { return ow; } });
      Object.defineProperty(window, "outerHeight", { configurable: true, get: function () { return oh; } });
      Object.defineProperty(window, "innerWidth", { configurable: true, get: function () { return iw; } });
      Object.defineProperty(window, "innerHeight", { configurable: true, get: function () { return ih; } });
      Object.defineProperty(window, "screenX", { configurable: true, get: function () { return cfg.screen_x || 0; } });
      Object.defineProperty(window, "screenY", { configurable: true, get: function () { return cfg.screen_y || 0; } });
      Object.defineProperty(window, "screenLeft", { configurable: true, get: function () { return cfg.screen_x || 0; } });
      Object.defineProperty(window, "screenTop", { configurable: true, get: function () { return cfg.screen_y || 0; } });
    } catch (e) {}
    // 移动端模拟：确保 documentElement/body 的 clientWidth 与 inner 尺寸一致
    try {
      Object.defineProperty(HTMLElement.prototype, "clientWidth", {
        configurable: true,
        get: function () {
          if (this === document.documentElement) return iw;
          // 其他元素保持原样：通过读取原属性描述符的 fallback
          // 这里做不到可靠的 fallback，就返回 this 的实际值。
          try { return Number(this.getBoundingClientRect().width) || iw; } catch (ee) { return iw; }
        },
      });
      Object.defineProperty(HTMLElement.prototype, "clientHeight", {
        configurable: true,
        get: function () {
          if (this === document.documentElement) return ih;
          try { return Number(this.getBoundingClientRect().height) || ih; } catch (ee) { return ih; }
        },
      });
    } catch (e) {}
  }
  _applyWindowSize();

  // --------------------------------------------------------------
  // 5) 设备属性：CPU 核心数、deviceMemory、maxTouchPoints、touch、platform、vendor
  // --------------------------------------------------------------
  try {
    Object.defineProperty(navigator, "hardwareConcurrency", {
      configurable: true,
      get: function () { return cfg.hardware_concurrency || 8; },
    });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, "deviceMemory", {
      configurable: true,
      get: function () { return cfg.device_memory || 8; },
    });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, "maxTouchPoints", {
      configurable: true,
      get: function () { return cfg.max_touch_points || 0; },
    });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, "platform", {
      configurable: true,
      get: function () { return cfg.platform || "Win32"; },
    });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, "vendor", {
      configurable: true,
      get: function () { return cfg.vendor || "Google Inc."; },
    });
  } catch (e) {}

  // ontouchstart：手机 profile 下挂一个空的事件处理器
  if (cfg.ontouchstart_present) {
    try { window.ontouchstart = function () {}; } catch (e) {}
  }

  // --------------------------------------------------------------
  // 6) 时区 / 语言：严格对齐 IP 归属地（默认 Asia/Shanghai + zh-CN）
  // --------------------------------------------------------------
  try {
    Object.defineProperty(navigator, "language", {
      configurable: true,
      get: function () { return cfg.language || "zh-CN"; },
    });
  } catch (e) {}
  try {
    Object.defineProperty(navigator, "languages", {
      configurable: true,
      get: function () { return cfg.languages || ["zh-CN", "zh", "en-US", "en"]; },
    });
  } catch (e) {}

  // 时区：hook Date.prototype.getTimezoneOffset + Intl.DateTimeFormat
  (function () {
    var tzName = cfg.timezone || "Asia/Shanghai";
    // 通过临时 Date 实例拿到目标时区相对 UTC 的偏移（分钟，返回负值表示东时区）
    try {
      var parts = new Intl.DateTimeFormat("en-US", {
        timeZone: tzName,
        hour12: false,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).formatToParts(new Date());
      var tzParts = {};
      for (var i = 0; i < parts.length; i++) tzParts[parts[i].type] = parts[i].value;
      // 构造一个 UTC 时间再在目标时区下解读
      var utcDate = new Date(
        parseInt(tzParts.year, 10),
        parseInt(tzParts.month, 10) - 1,
        parseInt(tzParts.day, 10),
        parseInt(tzParts.hour === "24" ? "00" : tzParts.hour, 10),
        parseInt(tzParts.minute, 10),
        parseInt(tzParts.second, 10)
      );
      var offsetMinutes = Math.round((utcDate.getTime() - Date.now()) / 60000);
      // 修正：实际 offsetMinutes 是 (UTC - local)；getTimezoneOffset 也是 (UTC - local)
      var realOffset = -offsetMinutes;

      var origGetTimezoneOffset = Date.prototype.getTimezoneOffset;
      Date.prototype.getTimezoneOffset = function () {
        // 不要完全静态——不同 Date 实例应得到一致偏移
        var thisOffset = origGetTimezoneOffset.call(this);
        if (typeof thisOffset === "number" && !isNaN(thisOffset) && Math.abs(thisOffset - realOffset) < 1440) {
          // 如果原生已经和目标接近（例如本机就是 Asia/Shanghai），直接返回原生结果，
          // 避免 hook 过度暴露
          return thisOffset;
        }
        return realOffset;
      };

      // Intl.DateTimeFormat：保留 API 行为，但覆盖 resolvedOptions().timeZone
      if (typeof Intl !== "undefined" && Intl.DateTimeFormat) {
        var origResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
        Intl.DateTimeFormat.prototype.resolvedOptions = function () {
          var opts = origResolvedOptions.call(this);
          try { opts.timeZone = tzName; } catch (e) {}
          return opts;
        };
      }
    } catch (e) {}
  })();

  // Accept-Language：通过 fetch/XHR 默认头无法直接覆盖，留作后端设置参考；
  // 这里只暴露 navigator 层面的语言信息，供 JS 端检测脚本读。

  // --------------------------------------------------------------
  // 7) 补全 navigator.plugins / navigator.mimeTypes
  // --------------------------------------------------------------
  (function () {
    var pluginDescs = [
      { name: "Chrome PDF Plugin", description: "Portable Document Format", filename: "pdf.dll", mime: "application/pdf" },
      { name: "Chrome PDF Viewer", description: "", filename: "mhjfbmdgcfjbbpaeojofohoefgiehjai", mime: "application/pdf" },
      { name: "Native Client", description: "", filename: "internal-nacl-plugin", mime: "application/x-nacl" },
    ];

    // 构造 plugin 数组 + mime 数组
    var plugins = [];
    var mimes = [];
    for (var i = 0; i < pluginDescs.length; i++) {
      var d = pluginDescs[i];
      var p = {
        name: d.name,
        description: d.description,
        filename: d.filename,
        version: "",
        length: 1,
      };
      p[0] = { type: d.mime, suffixes: "", description: d.description, enabledPlugin: p };
      p.item = function (idx) { return this[idx] || null; };
      p.namedItem = function (name) {
        for (var j = 0; j < this.length; j++) {
          if (this[j] && this[j].type === name) return this[j];
        }
        return null;
      };
      plugins.push(p);
      mimes.push({
        type: d.mime,
        suffixes: "",
        description: d.description,
        enabledPlugin: p,
      });
    }
    plugins.refresh = function () {};
    plugins.namedItem = function (name) {
      for (var k = 0; k < plugins.length; k++) {
        if (plugins[k].name === name) return plugins[k];
      }
      return null;
    };
    mimes.namedItem = function (name) {
      for (var k2 = 0; k2 < mimes.length; k2++) {
        if (mimes[k2].type === name) return mimes[k2];
      }
      return null;
    };

    try {
      Object.defineProperty(navigator, "plugins", {
        value: plugins,
        writable: false,
        configurable: true,
        enumerable: true,
      });
    } catch (e) {}
    try {
      Object.defineProperty(navigator, "mimeTypes", {
        value: mimes,
        writable: false,
        configurable: true,
        enumerable: true,
      });
    } catch (e) {}
  })();

  // --------------------------------------------------------------
  // 8) Canvas 指纹扰动：hook getImageData / toDataURL / toBlob，
  //    在返回前对每 N 个像素做 ±1 的 RGB 抖动，
  //    人眼不可见，但每次哈希不同。
  // --------------------------------------------------------------
  (function () {
    var rng = _makeRng(cfg.canvas_noise_seed || 12345);
    var enabled = true;

    function _applyImageDataNoise(imgData) {
      if (!enabled || !imgData || !imgData.data) return imgData;
      var data = imgData.data;
      var len = data.length;
      // 每 97 个通道值扰动一次（约 24 像素一次，足够稀疏，不影响肉眼）
      for (var i = 0; i < len; i += 4 * 97) {
        var delta = (rng() < 0.5 ? -1 : 1);
        var ch = Math.floor(rng() * 3); // 0 R, 1 G, 2 B，不动 A
        var idx = i + ch;
        if (idx >= len) break;
        var v = data[idx] + delta;
        if (v < 0) v = 0; else if (v > 255) v = 255;
        data[idx] = v;
      }
      return imgData;
    }

    // CanvasRenderingContext2D.getImageData
    try {
      var origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
      CanvasRenderingContext2D.prototype.getImageData = function (sx, sy, sw, sh) {
        var result = origGetImageData.apply(this, arguments);
        return _applyImageDataNoise(result);
      };
    } catch (e) {}

    // HTMLCanvasElement.toDataURL
    try {
      var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
      HTMLCanvasElement.prototype.toDataURL = function (type, quality) {
        // 先在画布上叠加 1 像素的随机噪点（极低 alpha）
        var w = this.width || 1;
        var h = this.height || 1;
        try {
          var ctx = this.getContext("2d");
          if (ctx) {
            var r = Math.floor(rng() * 255);
            var g = Math.floor(rng() * 255);
            var b = Math.floor(rng() * 255);
            var origAlpha = ctx.globalAlpha || 1;
            var origOp = ctx.globalCompositeOperation || "source-over";
            ctx.globalAlpha = 0.01;
            ctx.globalCompositeOperation = "source-over";
            ctx.fillStyle = "rgba(" + r + "," + g + "," + b + ",0.05)";
            // 在右下角 1 像素位置叠加一个不可见的点
            ctx.fillRect(w - 1, h - 1, 1, 1);
            ctx.globalAlpha = origAlpha;
            ctx.globalCompositeOperation = origOp;
          }
        } catch (e) {}
        return origToDataURL.apply(this, arguments);
      };
    } catch (e) {}

    // HTMLCanvasElement.toBlob
    try {
      var origToBlob = HTMLCanvasElement.prototype.toBlob;
      HTMLCanvasElement.prototype.toBlob = function (callback, type, quality) {
        var self = this;
        try {
          var ctx2 = self.getContext("2d");
          if (ctx2) {
            var w2 = self.width || 1;
            var h2 = self.height || 1;
            var r2 = Math.floor(rng() * 255);
            var g2 = Math.floor(rng() * 255);
            var b2 = Math.floor(rng() * 255);
            var origAlpha2 = ctx2.globalAlpha || 1;
            var origOp2 = ctx2.globalCompositeOperation || "source-over";
            ctx2.globalAlpha = 0.01;
            ctx2.globalCompositeOperation = "source-over";
            ctx2.fillStyle = "rgba(" + r2 + "," + g2 + "," + b2 + ",0.05)";
            ctx2.fillRect(w2 - 1, h2 - 1, 1, 1);
            ctx2.globalAlpha = origAlpha2;
            ctx2.globalCompositeOperation = origOp2;
          }
        } catch (e) {}
        return origToBlob.apply(this, arguments);
      };
    } catch (e) {}
  })();

  // --------------------------------------------------------------
  // 9) WebGL 指纹扰动：覆盖 getParameter 的 VENDOR/RENDERER/VERSION，
  //    并在 readPixels 结果里叠加极低幅度的噪声。
  //    注意：WebGL2 也一并处理。
  // --------------------------------------------------------------
  (function () {
    var rng = _makeRng(cfg.webgl_noise_seed || 67890);
    var fakeVendor = cfg.webgl_vendor || "Google Inc. (NVIDIA)";
    var fakeRenderer = cfg.webgl_renderer || "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-23.21.13.8792)";

    function _hookGetParameter(proto) {
      try {
        if (!proto || !proto.getParameter) return;
        var orig = proto.getParameter;
        proto.getParameter = function (pname) {
          try {
            // VENDOR = 0x1F00, RENDERER = 0x1F01, VERSION = 0x1F02
            if (pname === 0x1F00) return fakeVendor;
            if (pname === 0x1F01) return fakeRenderer;
          } catch (e) {}
          return orig.apply(this, arguments);
        };
      } catch (e) {}
    }

    function _hookGetExtension(proto) {
      try {
        if (!proto || !proto.getExtension) return;
        var origExt = proto.getExtension;
        proto.getExtension = function (name) {
          // 暴露常见扩展即可，不加伪造扩展暴露自己
          return origExt.apply(this, arguments);
        };
      } catch (e) {}
    }

    function _hookReadPixels(proto) {
      try {
        if (!proto || !proto.readPixels) return;
        var origRead = proto.readPixels;
        proto.readPixels = function (x, y, w, h, format, type, pixels) {
          var ret = origRead.apply(this, arguments);
          // 对 Uint8Array 类型的像素做 ±1 扰动（WebGL 默认 UNPACK 类型）
          if (pixels && pixels.length && pixels.BYTES_PER_ELEMENT === 1) {
            var arr = pixels;
            var step = 4 * 83;
            for (var i = 0; i < arr.length; i += step) {
              var d = (rng() < 0.5 ? -1 : 1);
              var v2 = arr[i] + d;
              if (v2 < 0) v2 = 0; else if (v2 > 255) v2 = 255;
              arr[i] = v2;
            }
          }
          return ret;
        };
      } catch (e) {}
    }

    if (typeof WebGLRenderingContext !== "undefined") {
      _hookGetParameter(WebGLRenderingContext.prototype);
      _hookGetExtension(WebGLRenderingContext.prototype);
      _hookReadPixels(WebGLRenderingContext.prototype);
    }
    if (typeof WebGL2RenderingContext !== "undefined") {
      _hookGetParameter(WebGL2RenderingContext.prototype);
      _hookGetExtension(WebGL2RenderingContext.prototype);
      _hookReadPixels(WebGL2RenderingContext.prototype);
    }

    // canvas.getContext：如果浏览器返回 webgl/webgl2/experimental-webgl，
    // 我们已经 hook 了原型；这里不需要二次处理。
  })();

  // --------------------------------------------------------------
  // 10) 音频指纹扰动：在 AudioBuffer.getChannelData 返回后叠加极低噪声，
  //     使基于 OscillatorNode 的设备指纹哈希不稳定。
  // --------------------------------------------------------------
  (function () {
    if (typeof AudioBuffer === "undefined") return;
    try {
      var rng = _makeRng(cfg.audio_noise_seed || 24680);
      var origGetChannelData = AudioBuffer.prototype.getChannelData;
      AudioBuffer.prototype.getChannelData = function (channel) {
        var data = origGetChannelData.apply(this, arguments);
        if (!data || !data.length) return data;
        // 每 2048 个采样点扰动 1 个，幅度 ±1e-6（极轻微，几乎不可听）
        try {
          var step = 2048;
          for (var i = 0; i < data.length; i += step) {
            var d = (rng() < 0.5 ? -1 : 1) * 0.000001;
            var v3 = data[i] + d;
            if (v3 < -1) v3 = -1; else if (v3 > 1) v3 = 1;
            data[i] = v3;
          }
        } catch (e) {}
        return data;
      };
    } catch (e) {}
  })();

  // --------------------------------------------------------------
  // 11) 字体指纹：限制可枚举字体数量，伪造常见字体列表。
  //     策略：暴露一份 40-60 个常见中英字体（cfg.fonts），
  //     检测脚本测"字体宽度是否与系统字体一致"时，
  //     由于实际安装字体不同会自然产生差异——我们不强改字形，
  //     只保证返回的字体数量处于"典型用户"范围。
  // --------------------------------------------------------------
  (function () {
    // 这里主要依赖 fingerprint_config.py 里 fonts 字段；
    // JS 端可暴露一个简单指纹描述对象（不侵入 DOM 字体测量本身），
    // 目的是保证 navigator.fonts 与字体枚举数量稳定。
    // 如果检测方用 DOM + canvas 测量字形，Canvas 扰动已经会使其哈希不稳定。
    try {
      if (typeof window !== "undefined" && cfg.fonts && cfg.fonts.length) {
        Object.defineProperty(window, "__fp_fonts__", {
          value: cfg.fonts.slice(0, 60),
          writable: false,
          configurable: true,
          enumerable: false,
        });
      }
    } catch (e) {}
  })();

  // --------------------------------------------------------------
  // 12) 地理信息：默认覆盖 navigator.geolocation，让检测脚本要么
  //     拿不到坐标，要么拿到一个与 IP 大致对齐的中国坐标（如上海附近）。
  // --------------------------------------------------------------
  (function () {
    if (typeof navigator === "undefined" || !navigator.geolocation) return;
    var defaultLat = 31.2304;
    var defaultLng = 121.4737; // 上海
    try {
      var geo = navigator.geolocation;
      var origGet = geo.getCurrentPosition;
      var origWatch = geo.watchPosition;

      function _fakePosition() {
        return {
          coords: {
            latitude: defaultLat + (Math.random() - 0.5) * 0.02,
            longitude: defaultLng + (Math.random() - 0.5) * 0.02,
            altitude: null,
            accuracy: 30 + Math.random() * 20,
            altitudeAccuracy: null,
            heading: null,
            speed: null,
          },
          timestamp: Date.now(),
        };
      }

      try {
        geo.getCurrentPosition = function (success, error, options) {
          // 如果没有明确授权，则返回"未授权"以避免暴露真实坐标；
          // 若一定要返回位置，则使用上海附近的伪坐标。
          if (success) setTimeout(function () { success(_fakePosition()); }, 0);
        };
        geo.watchPosition = function (success, error, options) {
          if (success) {
            var id = Math.floor(Math.random() * 1e9);
            setTimeout(function () { success(_fakePosition()); }, 0);
            return id;
          }
          return 0;
        };
      } catch (e) {}
    } catch (e) {}
  })();

  // --------------------------------------------------------------
  // 13) WebRTC 禁用：hook RTCPeerConnection，让任何建立 WebRTC 的
  //     尝试都失败，避免真实 IP 通过 SDP 的 ICE candidate 泄露。
  // --------------------------------------------------------------
  (function () {
    function _disable(name) {
      try {
        var ctor = window[name];
        if (!ctor) return;
        // 覆盖原型方法，让 createOffer / createAnswer / addIceCandidate 直接 reject
        var proto = ctor.prototype;
        if (proto && proto.createOffer) {
          proto.createOffer = function () {
            return Promise.reject(new Error("WebRTC is disabled"));
          };
        }
        if (proto && proto.createAnswer) {
          proto.createAnswer = function () {
            return Promise.reject(new Error("WebRTC is disabled"));
          };
        }
        if (proto && proto.addIceCandidate) {
          proto.addIceCandidate = function () {
            return Promise.reject(new Error("WebRTC is disabled"));
          };
        }
      } catch (e) {}
    }
    _disable("RTCPeerConnection");
    _disable("webkitRTCPeerConnection");
    _disable("mozRTCPeerConnection");

    // getUserMedia / getDisplayMedia：保留（是摄像头/屏幕共享），
    // 不主动禁用以免影响正常功能。只有 WebRTC 需要屏蔽 IP 泄露。
  })();
})();