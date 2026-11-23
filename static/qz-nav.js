/* 群智 · 统一业务线导航组件（面包屑 + 底栏 nav-foot）
   用法：在页面 </body> 前加一行 <script src="/static/qz-nav.js"></script>
   组件自动识别 5 条业务线、当前步骤、pid/job/topic 状态，并写入 localStorage：
     qz_last   = 上次走到哪（workbench「继续上次」）
     qz_recent = 最近任务（workbench「最近 4 条」）
   需要动态改步骤时调用 window.QZNav.setStep / setNext / setTitle。
*/
(function () {
  "use strict";

  var PATH = (location.pathname || "/").replace(/\/+$/, "") || "/";
  var P = new URLSearchParams(location.search);

  var LINES = {
    1: { name: "单篇体检", steps: ["选论文", "看证据", "读原文", "改进建议"], start: "/kb/selfcheck" },
    2: { name: "整包检查", steps: ["上传申报包", "分诊结果", "数字对账", "评审意见"], start: "/pkg" },
    3: { name: "文献工作", steps: ["检索", "读原文", "搭骨架", "查漏引"], start: "/kb" },
    4: { name: "选题评估", steps: ["输入选题", "评估结果", "转成骨架"], start: "/judge" },
    5: { name: "同行基线", steps: ["选领域", "20 项分布"], start: "/kb/baseline" }
  };

  var override = { next: null, step: null, secondary: null };

  function enc(v) { return encodeURIComponent(String(v == null ? "" : v)); }
  function int(v) { var n = parseInt(v, 10); return isFinite(n) ? n : 0; }
  function val(id) {
    var el = document.getElementById(id);
    return el && typeof el.value === "string" ? el.value.trim() : "";
  }
  function mk(base, params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === "") return;
      parts.push(enc(k) + "=" + enc(v));
    });
    return base + (parts.length ? "?" + parts.join("&") : "");
  }
  function getPid() {
    var pid = P.get("pid") || val("pid") || "";
    if (!pid && PATH === "/kb") pid = localStorage.getItem("qz.pid") || "";
    return pid;
  }
  function getTopic() {
    return P.get("topic") || val("topic") || val("doc-title") || "";
  }

  var pkgMatch = PATH.match(/^\/pkg\/([^/]+)(?:\/(numbers|review|review2))?$/);
  var JOB = P.get("job") || (pkgMatch ? pkgMatch[1] : "");
  var PKG_KIND = pkgMatch ? (pkgMatch[2] || "view") : "";

  function detectLine() {
    if (PATH === "/evidence" || PATH === "/kb/selfcheck" || PATH === "/read") {
      // /read 的 line 现在是“证据条目序号”（QBUG-001），业务线改用 biz。
      var ln = PATH === "/read" ? int(P.get("biz")) : int(P.get("line"));
      if (ln) return ln;
      var ref = document.referrer || "";
      if (PATH === "/read" && /\/evidence/.test(ref)) return 1;
      if (PATH === "/read" && /\/kb\/selfcheck/.test(ref)) return 1;
      if (PATH === "/read" && /\/kb(\/|$|\?)/.test(ref)) return 3;
      return 1;
    }
    if (PATH === "/outline") {
      var l2 = int(P.get("line"));
      if (l2) return l2;
      return P.get("topic") ? 4 : 1;
    }
    if (PATH === "/kb" || PATH === "/kb/missing") return 3;
    if (PATH === "/kb/baseline") return 5;
    if (PATH === "/pkg" || /^\/pkg\//.test(PATH)) return 2;
    if (PATH === "/judge") return 4;
    return 0;
  }

  /* 返回当前页的线 / 步骤 / 上一步 / 下一步 / 副按钮 */
  function build() {
    var pid = getPid(), topic = getTopic(), line = detectLine();
    var idx = 0, prev = null, next = null, secondary = null;
    var v = { pid: pid, topic: topic, job: JOB, line: line };

    if (PATH === "/portal") {
      v.lineName = "入口"; v.stepName = "5 条业务线"; v.idx = null; v.total = null;
      v.prev = null;
      v.next = { href: "/workbench", label: "工作台" };
      return v;
    }
    if (PATH === "/workbench") {
      v.lineName = "工作台"; v.stepName = "数据面板"; v.idx = null; v.total = null;
      v.prev = { href: "/portal", label: "5 条业务线" };
      v.next = { href: "/portal", label: "回门户" };
      return v;
    }
    if (PATH === "/gates") {
      v.lineName = "四关一审"; v.stepName = "5 个闸门"; v.idx = null; v.total = null;
      v.prev = { href: "/portal", label: "回门户" };
      v.next = { href: "/workbench", label: "工作台" };
      return v;
    }
    if (PATH === "/eval") {
      v.lineName = "评测面板"; v.stepName = "硬数字"; v.idx = null; v.total = null;
      v.lineHref = "/portal";
      v.prev = { href: "/portal", label: "回门户" };
      v.next = { href: "/workbench", label: "工作台" };
      return v;
    }
    if (PATH === "/admin") {
      v.lineName = "管理控制台"; v.stepName = "Key / 用量 / 任务"; v.idx = null; v.total = null;
      v.lineHref = "/portal";
      v.prev = { href: "/portal", label: "回门户" };
      v.next = { href: "/workbench", label: "工作台" };
      return v;
    }
    if (PATH === "/tools") {
      v.lineName = "能力平台"; v.stepName = "工具清单"; v.idx = null; v.total = null;
      v.lineHref = "/portal";
      v.prev = { href: "/portal", label: "回门户" };
      v.next = { href: "/workbench", label: "工作台" };
      return v;
    }
    if (PATH === "/runs") {
      v.lineName = "任务历史"; v.stepName = "论文包 / agent 运行"; v.idx = null; v.total = null;
      v.lineHref = "/portal";
      v.prev = { href: "/workbench", label: "工作台" };
      v.next = null;
      return v;
    }

    var L = LINES[line] || LINES[1];
    v.lineName = L.name;

    if (PATH === "/kb/selfcheck") {
      idx = 0;
      prev = { href: "/portal", label: "回门户" };
      next = pid ? { href: mk("/evidence", { pid: pid, line: 1, auto: 1 }), label: "看证据" } : null;
    } else if (PATH === "/evidence") {
      idx = 1;
      prev = { href: mk("/kb/selfcheck", { pid: pid }), label: "选论文" };
      next = pid ? { href: mk("/read", { pid: pid, biz: line }), label: "读原文" } : null;
    } else if (PATH === "/read") {
      idx = line === 3 ? 1 : 2;
      prev = line === 3
        ? { href: "/kb", label: "检索" }
        : { href: mk("/evidence", { pid: pid, line: 1, auto: 1 }), label: "看证据" };
      next = pid ? { href: mk("/outline", { pid: pid, line: line }), label: "改进建议" } : null;
      if (line === 3 && next) next.label = "搭骨架";
    } else if (PATH === "/outline") {
      idx = line === 3 ? 2 : (line === 4 ? 2 : 3);
      if (line === 3) {
        prev = { href: mk("/read", { pid: pid, biz: 3 }), label: "读原文" };
        next = pid ? { href: mk("/kb/missing", { pid: pid, line: 3, auto: 1 }), label: "查漏引" } : null;
      } else if (line === 4) {
        prev = { href: mk("/judge", { topic: topic }), label: "评估结果" };
        next = null;
      } else {
        prev = { href: mk("/read", { pid: pid, biz: 1 }), label: "读原文" };
        next = null;
      }
    } else if (PATH === "/kb") {
      idx = 0;
      prev = { href: "/portal", label: "回门户" };
      next = pid ? { href: mk("/read", { pid: pid, biz: 3 }), label: "读原文" } : null;
    } else if (PATH === "/kb/baseline") {
      var dist = P.get("view") === "dist";
      idx = dist ? 1 : 0;
      prev = dist ? { href: "/kb/baseline", label: "选领域" } : { href: "/portal", label: "回门户" };
      next = dist
        ? { href: mk("/kb/selfcheck", { line: 1 }), label: "去查自己的论文" }
        : { href: "/kb/baseline?view=dist#rows", label: "看 20 项分布" };
    } else if (PATH === "/kb/missing") {
      idx = 3;
      prev = { href: mk("/read", { pid: pid, biz: 3 }), label: "读原文" };
      next = null;
    } else if (PATH === "/pkg") {
      idx = 0;
      prev = { href: "/portal", label: "回门户" };
      next = null;
    } else if (/^\/pkg\//.test(PATH)) {
      if (PKG_KIND === "view") {
        idx = 1;
        prev = { href: "/pkg", label: "上传申报包" };
        next = { href: mk("/pkg/" + enc(JOB) + "/numbers", { job: JOB }), label: "数字对账", run: "numbers" };
        secondary = { href: mk("/pkg/" + enc(JOB) + "/review", { job: JOB }), label: "查看报告", run: "review", id: "qznSub" };
      } else if (PKG_KIND === "numbers") {
        idx = 2;
        prev = { href: mk("/pkg/" + enc(JOB), { job: JOB }), label: "分诊结果" };
        next = { href: mk("/pkg/" + enc(JOB) + "/review", { job: JOB }), label: "评审意见", run: "review" };
        secondary = { href: PATH + location.search, label: "查看报告", blank: true, id: "qznSub" };
      } else {
        idx = 3;
        prev = { href: mk("/pkg/" + enc(JOB) + "/numbers", { job: JOB }), label: "数字对账" };
        next = null;
      }
    } else if (PATH === "/judge") {
      idx = 0;
      prev = { href: "/portal", label: "回门户" };
      next = { href: "#evalCard", label: "输入选题", focus: "topic" };
    } else {
      idx = 0;
      prev = { href: "/portal", label: "回门户" };
    }

    v.idx = idx;
    v.total = L.steps.length;
    v.stepName = L.steps[idx] || L.steps[0];
    v.prev = prev;
    v.next = next;
    v.secondary = secondary;
    return v;
  }

  /* ---------- 渲染 ---------- */
  var cssLoaded = false;
  function ensureCss() {
    if (cssLoaded || document.getElementById("qznCss")) { cssLoaded = true; return; }
    var l = document.createElement("link");
    l.id = "qznCss"; l.rel = "stylesheet"; l.href = "/static/qz-nav.css";
    document.head.appendChild(l);
    cssLoaded = true;
  }

  /* ---------- 顶部服务在线（Bug6：真查 /api/lib/health_live，30s 刷新） ---------- */
  function ensureHealth(crumb) {
    // 工作台有自己的 #livePill；避免同一屏两个状态
    if (document.getElementById("livePill")) return null;
    var el = document.getElementById("qznHealth");
    if (!el) {
      el = document.createElement("span");
      el.id = "qznHealth";
      el.className = "qzn-health";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.innerHTML = '<i class="d"></i><span>检查中…</span>';
      crumb.appendChild(el);
    }
    return el;
  }

  function checkHealth() {
    if (document.getElementById("livePill")) return;
    var el = document.getElementById("qznHealth");
    if (!el) return;
    var label = el.querySelector("span");
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 12000) : null;
    fetch("/api/lib/health_live?probe=1", {
      cache: "no-store",
      headers: { "X-Access-Key": "__QZ_DEMO_KEY__" },
      signal: ctrl ? ctrl.signal : undefined
    }).then(function (r) {
      if (timer) clearTimeout(timer);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (j) {
      if (!j || j.ok === false) throw new Error((j && j.error) || "返回异常");
      el.className = "qzn-health ok";
      if (label) label.textContent = "服务在线";
      el.title = "/api/lib/health_live 探测成功 · " +
        new Date().toLocaleTimeString("zh-CN", { hour12: false });
    }).catch(function (e) {
      if (timer) clearTimeout(timer);
      el.className = "qzn-health bad";
      if (label) label.textContent = "服务离线";
      el.title = "无法访问 /api/lib/health_live：" + (e && e.message ? e.message : "unknown");
    });
  }

  function startHealth() {
    ensureHealth(document.getElementById("qznCrumb") || ensureCrumb());
    checkHealth();
    if (!window.__qznHealthTimer) window.__qznHealthTimer = setInterval(checkHealth, 30000);
  }

  function ensureCrumb() {
    var el = document.getElementById("qznCrumb");
    if (el) return el;
    el = document.createElement("div");
    el.id = "qznCrumb"; el.className = "qzn-crumb";
    document.body.insertBefore(el, document.body.firstChild);
    return el;
  }

  /* ---------- 移动端菜单（QBUG-002）----------
     ≤760px 时在各页顶部面包屑右侧显示「☰ 导航」，
     展开 5 条业务线 + 工作台 + 门户；不依赖各页自己的 nav。 */
  var MOBILE_LINKS = [
    ["/workbench", "工作台", "数据面板 / 继续上次"],
    ["/kb/selfcheck", "单篇体检", "选论文 → 看证据 → 读原文"],
    ["/pkg", "整包检查", "上传申报包 → 对账"],
    ["/kb", "文献工作", "检索 → 读原文 → 搭骨架"],
    ["/judge", "选题评估", "输入选题 → 评估"],
    ["/kb/baseline", "同行基线", "选领域 → 20 项分布"],
    ["/portal", "回门户", "5 条业务线入口"]
  ];

  function ensureBurger(crumb) {
    var old = document.getElementById("qznBurger");
    if (old) old.parentNode && old.parentNode.removeChild(old);
    var b = document.createElement("button");
    b.id = "qznBurger";
    b.className = "qzn-burger";
    b.type = "button";
    b.setAttribute("aria-label", "打开业务线导航");
    b.setAttribute("aria-expanded", "false");
    b.setAttribute("aria-controls", "qznMenu");
    b.innerHTML = "☰ 导航";
    crumb.appendChild(b);
  }

  function ensureMenu() {
    var m = document.getElementById("qznMenu");
    if (m) return m;
    m = document.createElement("div");
    m.id = "qznMenu";
    m.className = "qzn-menu";
    m.hidden = true;
    m.innerHTML = '<div class="qzn-menu-h">业务线导航</div><div class="qzn-menu-grid">' +
      MOBILE_LINKS.map(function (x) {
        return '<a href="' + x[0] + '"><b>' + x[1] + '</b><span>' + x[2] + '</span></a>';
      }).join("") + '</div>';
    document.body.appendChild(m);
    return m;
  }

  function setMenu(open) {
    var m = ensureMenu();
    var b = document.getElementById("qznBurger");
    m.hidden = !open;
    if (b) b.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var first = m.querySelector("a");
      if (first && window.matchMedia && window.matchMedia("(max-width:760px)").matches) {
        try { first.focus({ preventScroll: true }); } catch (e) {}
      }
    }
  }

  function ensureFoot() {
    var el = document.getElementById("qznFoot");
    if (el) return el;
    el = document.createElement("nav");
    el.id = "qznFoot"; el.className = "qzn-foot";
    el.setAttribute("aria-label", "业务线步骤导航");
    el.innerHTML = '<div class="qzn-foot-in">' +
      '<a class="qzn-btn ghost" id="qznPrev" href="#"></a>' +
      '<div class="qzn-mid" id="qznMid"></div>' +
      '<div class="qzn-right" id="qznRight"></div>' +
      '</div>';
    document.body.appendChild(el);
    return el;
  }

  function bindRun(a, v) {
    if (!a || !a.getAttribute("data-run")) return;
    a.addEventListener("click", function (ev) {
      ev.preventDefault();
      if (a.getAttribute("data-busy")) return;
      var href = a.getAttribute("href");
      var old = a.innerHTML;
      a.setAttribute("data-busy", "1");
      a.classList.add("qzn-busy");
      a.innerHTML = "正在生成报告…";
      fetch(href, { method: "GET", cache: "no-store" }).then(function (probe) {
        if (probe.ok) return null;
        var api = "/api/pkg/" + enc(a.getAttribute("data-job") || v.job) + "/" + a.getAttribute("data-run") + "?key=__QZ_DEMO_KEY__";
        return fetch(api, { method: "POST" }).then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            if (!r.ok) throw new Error(j.detail || ("HTTP " + r.status));
            return null;
          });
        });
      }).then(function () {
        location.href = href;
      }).catch(function (err) {
        alert("生成报告失败：" + (err && err.message ? err.message : err));
        a.removeAttribute("data-busy");
        a.classList.remove("qzn-busy");
        a.innerHTML = old;
      });
    });
  }

  function applyOverrides(v) {
    if (override.next) v.next = override.next;
    if (override.secondary) v.secondary = override.secondary;
    if (override.step) {
      v.stepName = override.step.name || v.stepName;
      v.idx = (override.step.idx == null ? v.idx : override.step.idx);
      v.total = override.step.total || v.total;
      v.lineName = override.step.lineName || v.lineName;
    }
    return v;
  }

  function render() {
    ensureCss();
    var v = applyOverrides(build());

    /* 面包屑 */
    var c = ensureCrumb();
    var crumb = '<a class="qzn-brand" href="/portal">群智</a><span class="qzn-sep">›</span>';
    if (PATH === "/portal") {
      crumb += '<span class="qzn-cur">入口</span><span class="qzn-sep">›</span><span>5 条业务线</span>';
    } else if (PATH === "/workbench") {
      crumb += '<a href="/portal">工作台</a><span class="qzn-sep">›</span><span class="qzn-cur">数据面板</span>';
    } else if (PATH === "/gates") {
      crumb += '<span class="qzn-cur">四关一审</span><span class="qzn-sep">›</span><span>5 个闸门</span>';
    } else {
      var lineHref = v.lineHref || (LINES[v.line] || LINES[1]).start;
      crumb += '<a href="' + lineHref + '">' + v.lineName + '</a><span class="qzn-sep">›</span>' +
        '<span class="qzn-cur">' + v.stepName + '</span>';
    }
    if (v.idx != null && v.total) {
      crumb += '<span class="qzn-stepno">第 ' + (v.idx + 1) + ' / ' + v.total + ' 步</span>';
    } else if (PATH === "/portal") {
      crumb += '<span class="qzn-stepno">5 条业务线 · 每条从第 1 步走到最后一步</span>';
    } else if (PATH === "/workbench") {
      crumb += '<span class="qzn-stepno">数据面板 + 5 条业务线</span>';
    } else if (PATH === "/gates") {
      crumb += '<span class="qzn-stepno">实时探测 + 缓存快照</span>';
    }
    c.innerHTML = crumb;
    ensureHealth(c);
    ensureBurger(c);

    /* 底栏 */
    ensureFoot();
    document.documentElement.classList.add("qzn-has-foot");

    var prevA = document.getElementById("qznPrev");
    if (v.prev && v.prev.href) {
      prevA.setAttribute("href", v.prev.href);
      prevA.setAttribute("aria-disabled", "false");
      prevA.innerHTML = '← 上一步<span class="qzn-sub">：' + v.prev.label + '</span>';
    } else {
      prevA.setAttribute("href", "javascript:void(0)");
      prevA.setAttribute("aria-disabled", "true");
      prevA.innerHTML = '← 上一步';
    }

    var mid = document.getElementById("qznMid");
    mid.innerHTML = "<b>" + v.lineName + "</b><span>" +
      (v.idx != null && v.total ? ("第 " + (v.idx + 1) + " / " + v.total + " 步 · ") : "") +
      v.stepName + "</span>";

    var right = document.getElementById("qznRight");
    var html = "";
    if (v.secondary) {
      html += '<a class="qzn-btn subtle" id="' + (v.secondary.id || "qznSub") + '" href="' + v.secondary.href + '"' +
        (v.secondary.blank ? ' target="_blank" rel="noopener"' : '') +
        (v.secondary.run ? ' data-run="' + v.secondary.run + '" data-job="' + enc(v.job) + '"' : '') +
        '>' + v.secondary.label + ' →</a>';
    }
    var prefix = v.line === 2 ? "继续检查" : "下一步";
    if (v.next) {
      html += '<a class="qzn-btn primary" id="qznNext" href="' + v.next.href + '"' +
        (v.next.run ? ' data-run="' + v.next.run + '" data-job="' + enc(v.job) + '"' : '') +
        '>' + prefix + '：' + v.next.label + ' →</a>';
    } else if (PATH === "/workbench") {
      html += '<a class="qzn-btn primary" id="qznNext" href="/portal">回门户 →</a>';
    } else {
      html += '<a class="qzn-btn primary" id="qznNext" href="/workbench">回到工作台 →</a>';
    }
    right.innerHTML = html;

    var nextA = document.getElementById("qznNext");
    bindRun(nextA, v);
    if (v.next && v.next.focus) {
      nextA.addEventListener("click", function (ev) {
        var t = document.getElementById(v.next.focus);
        if (!t) return;
        ev.preventDefault();
        t.scrollIntoView({ behavior: "smooth", block: "center" });
        setTimeout(function () { try { t.focus(); } catch (e) {} }, 260);
      });
    }
    if (v.secondary && v.secondary.run) {
      bindRun(document.getElementById(v.secondary.id || "qznSub"), v);
    }

    record(v);
  }

  /* ---------- localStorage：上次走到哪 / 最近任务 ---------- */
  function pageTitle() {
    var h1 = document.querySelector("h1");
    var t = h1 ? h1.textContent : "";
    if (!t || /加载中|\.\.\.|…/.test(t)) t = document.title || "";
    t = t.replace(/\s*[·｜]\s*群智.*$/, "").replace(/\s+/g, " ").trim();
    return t.slice(0, 60);
  }

  function record(v) {
    if (PATH === "/portal" || PATH === "/workbench") return;
    try {
      var href = PATH + (location.search || "");
      var item = {
        href: href,
        title: pageTitle() || v.stepName,
        line: v.lineName,
        step: v.stepName,
        meta: v.lineName + " › " + v.stepName,
        t: Date.now()
      };
      var arr = [];
      try { arr = JSON.parse(localStorage.getItem("qz_recent") || "[]") || []; } catch (e) { arr = []; }
      if (!Array.isArray(arr)) arr = [];
      arr = arr.filter(function (x) { return x && x.href !== href; });
      arr.unshift(item);
      localStorage.setItem("qz_recent", JSON.stringify(arr.slice(0, 8)));
      localStorage.setItem("qz_last", JSON.stringify(item));
      if (v.pid) localStorage.setItem("qz.pid", v.pid);
      if (v.job) localStorage.setItem("qz.job", v.job);
    } catch (e) { /* 隐私模式下 localStorage 可能不可用 */ }
  }

  function setTitle(title) {
    if (!title) return;
    try {
      var href = PATH + (location.search || "");
      var arr = JSON.parse(localStorage.getItem("qz_recent") || "[]") || [];
      if (Array.isArray(arr)) {
        arr = arr.map(function (x) {
          if (x && x.href === href) x.title = String(title).slice(0, 80);
          return x;
        });
        localStorage.setItem("qz_recent", JSON.stringify(arr.slice(0, 8)));
      }
      var last = JSON.parse(localStorage.getItem("qz_last") || "null");
      if (last && last.href === href) {
        last.title = String(title).slice(0, 80);
        localStorage.setItem("qz_last", JSON.stringify(last));
      }
    } catch (e) {}
  }

  /* ---------- 对外 API ---------- */
  window.QZNav = {
    refresh: render,
    setNext: function (href, label, opts) {
      override.next = { href: href, label: label || "下一步",
        run: (opts && opts.run) || null, blank: !!(opts && opts.blank) };
      render();
    },
    setSecondary: function (href, label, opts) {
      override.secondary = href ? { href: href, label: label || "查看报告",
        run: (opts && opts.run) || null, blank: !!(opts && opts.blank) } : null;
      render();
    },
    setStep: function (name, idx, total, lineName) {
      override.step = { name: name, idx: idx, total: total, lineName: lineName || null };
      render();
    },
    setTitle: setTitle,
    setJob: function (job) { JOB = job || ""; render(); },
    state: function () { return applyOverrides(build()); }
  };

  /* 菜单交互：事件委托，避免每次 render 重建后丢监听 */
  document.addEventListener("click", function (e) {
    var burger = e.target && e.target.closest ? e.target.closest("#qznBurger") : null;
    if (burger) {
      e.preventDefault();
      var m = ensureMenu();
      setMenu(m.hidden);
      return;
    }
    var menu = document.getElementById("qznMenu");
    if (menu && !menu.hidden) {
      if (e.target.closest && e.target.closest("#qznMenu a")) { setMenu(false); return; }
      if (!e.target.closest || !e.target.closest("#qznMenu")) setMenu(false);
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") setMenu(false);
  });

  /* ---------- 启动 / 动态更新 ---------- */
  function schedule() {
    if (window.__qznRaf) cancelAnimationFrame(window.__qznRaf);
    window.__qznRaf = requestAnimationFrame(function () { window.__qznRaf = null; render(); });
  }

  document.addEventListener("input", function (e) {
    var id = e.target && e.target.id;
    if (id === "pid" || id === "topic" || id === "doc-title") schedule();
  }, true);
  document.addEventListener("change", function (e) {
    var id = e.target && e.target.id;
    if (id === "pid" || id === "topic" || id === "doc-title") schedule();
  }, true);
  window.addEventListener("load", function () {
    setTimeout(render, 60);
    setTimeout(startHealth, 420);
  });

  // 记住用户点过的 pid（供 /kb 的「下一步：读原文」使用）
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    var m = href.match(/[?&]pid=([^&]+)/);
    if (m) {
      try { localStorage.setItem("qz.pid", decodeURIComponent(m[1])); } catch (err) {}
      setTimeout(schedule, 0);
    }
  }, true);

  if (document.body) render();
  else document.addEventListener("DOMContentLoaded", render);
})();
