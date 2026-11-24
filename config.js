/* 群智 local · 配置加载
 * ---------------------------------------------------------------
 * 默认配置直接写在这里，保证「双击 index.html、断网」也能跑纯本地规则。
 *
 * 想用 config.json：
 *   1. 复制 config.example.json 为 config.json；
 *   2. 用本地静态服务器打开（python -m http.server 8000），
 *      浏览器不允许 file:// 页面读取同目录 JSON，这是浏览器安全策略；
 *   3. 访问 http://localhost:8000/?config=1 —— 加 ?config=1 才读 config.json。
 *      默认不读，是为了避免没放 config.json 时控制台出现一条 404。
 *   4. config.json 会覆盖下面的默认值；页面里保存过的设置优先级更高。
 *
 * 隐私：config.json 里如果填了 LLM key，请不要提交到 git（.gitignore 已忽略）。
 *       key 只存在你的机器上，请求由浏览器直连 LLM 服务商。
 */
(function () {
  "use strict";

  var DEFAULTS = {
    llm: {
      provider: "deepseek",
      base_url: "https://api.deepseek.com/v1",
      api_key: "",
      model: "deepseek-chat"
    },
    cloud_api: {
      base_url: "https://qunzhi.team:8443/api/pub",
      enabled: true
    }
  };

  function merge(base, extra) {
    var out = JSON.parse(JSON.stringify(base));
    if (!extra || typeof extra !== "object") return out;
    Object.keys(extra).forEach(function (k) {
      if (extra[k] && typeof extra[k] === "object" && !Array.isArray(extra[k])) {
        out[k] = merge(out[k] || {}, extra[k]);
      } else if (extra[k] !== undefined && extra[k] !== null) {
        out[k] = extra[k];
      }
    });
    return out;
  }

  window.QZ_DEFAULTS = DEFAULTS;
  window.QZ_CONFIG = merge(DEFAULTS, {});
  window.QZ_CONFIG_SOURCE = "config.js 默认值";

  // file:// 下不发任何请求（包括 config.json），保证「纯本地 = 0 请求」；
  // http(s) 下也要显式 ?config=1 才读，避免默认出现 404。
  var wantConfig = /[?&]config=1(?:&|$)/.test(location.search);
  if (wantConfig && (location.protocol === "http:" || location.protocol === "https:")) {
    window.QZ_CONFIG_READY = fetch("./config.json", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (j) {
          window.QZ_CONFIG = merge(DEFAULTS, j);
          window.QZ_CONFIG_SOURCE = "config.json";
        }
        return window.QZ_CONFIG;
      })
      .catch(function () { return window.QZ_CONFIG; });
  } else {
    window.QZ_CONFIG_READY = Promise.resolve(window.QZ_CONFIG);
  }
})();
