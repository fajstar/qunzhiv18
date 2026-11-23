/* 群智 · 共享脚本（微组件：escape / 请求 / toast / 时间格式化） */
window.qz = {
  esc(s){ return (s||"").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c])); },
  async api(path, opts) {
    const r = await fetch(path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j && (j.detail || (j.error && j.error.message))) || ("HTTP " + r.status));
    return j;
  },
  toast(msg) {
    const d = document.createElement("div");
    d.textContent = msg;
    d.style.cssText = "position:fixed;left:50%;bottom:28px;transform:translateX(-50%);background:#1F2937;color:#fff;padding:10px 18px;border-radius:20px;font-size:13px;z-index:99999;opacity:0;transition:.25s";
    document.body.appendChild(d);
    requestAnimationFrame(() => d.style.opacity = "1");
    setTimeout(() => { d.style.opacity = "0"; setTimeout(() => d.remove(), 300); }, 1800);
  },
  fmtTime(sec) { return sec >= 60 ? Math.round(sec / 60) + " 分 " + Math.round(sec % 60) + " 秒" : Math.round(sec) + " 秒"; },
  num(n) { return (n == null || isNaN(n)) ? "—" : Number(n).toLocaleString("zh-CN"); },
  wan(n) { return (n == null || isNaN(n)) ? "—" : (Number(n) / 10000).toFixed(1) + " 万"; },
  // call_id 徽章：hover 显示全 id，点击打开 /api/call/{id} 回放（BUG-005）
  callBadge(meta, label) {
    const cid = meta && meta.call_id;
    if (!cid) return "";
    const short = String(cid).replace(/^qz_\d+_/, "");
    return '<a class="qz-call" href="/api/call/' + encodeURIComponent(cid) +
      '" target="_blank" rel="noopener" title="call_id: ' + this.esc(cid) +
      ' · 点击回放">' + this.esc(label || "call_id") + " " + this.esc(short) + "</a>";
  },
  boot() { /* 预留：注入公共头/埋点 */ }
};
