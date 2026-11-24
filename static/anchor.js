/* 群智 local · T1/T2/T3 锚定
 * ---------------------------------------------------------------
 * 锚定 = 把一条结论/问题钉回原文的某一句话，让读者能自己核对。
 *
 *   T1  逐字命中：引文在原文中逐字存在，且长度 ≥ 40 字符（归一化空白后）
 *   T2  部分命中：只找到片段/短句，或只能锚到相关章节标题
 *   T3  无依据：在原文里找不到可核对的文字 —— 这类条目会被前端直接拦掉，不展示为结论
 *
 * 注意：T1 只证明「这句话确实在原文里」，不证明「这句话的解读正确」。
 *       解读仍需人来确认。
 */
(function (root) {
  "use strict";

  const REF_HEAD = /^[ \t]*(references|bibliography|参考文献|引用文献)[ \t]*$/im;
  const HEADING = /^[ \t]*(?:\d+(?:\.\d+)*[.)]?\s+)?([A-Z][^\n]{2,70}|[\u4e00-\u9fff][^\n]{1,40})[ \t]*$/gm;
  const T1_MIN = 40;

  function normWs(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  /** 去掉空白与小写，同时记录每个归一化字符在原文中的下标。 */
  function compact(text) {
    let out = "";
    const map = [];
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (/\s/.test(ch)) continue;
      out += ch.toLowerCase();
      map.push(i);
    }
    return { s: out, map: map };
  }

  function findQuote(text, quote) {
    const q = (quote || "").replace(/^[\s"'“”‘’「」『』()（）.,;:，。；：]+|[\s"'“”‘’「」『』()（）.,;:，。；：]+$/g, "");
    if (!q) return null;
    const hay = compact(text);
    const needle = q.replace(/\s+/g, "").toLowerCase();
    if (!needle) return null;
    const idx = hay.s.indexOf(needle);
    if (idx < 0) return null;
    const start = hay.map[idx];
    const end = hay.map[idx + needle.length - 1] + 1;
    return { start: start, end: end, text: text.slice(start, end), exact: true };
  }

  // 句末判定：中文标点/问叹/换行总是边界；英文句点要排除域名、小数、缩写、 initials。
  const ABBR = /(?:e\.g|i\.e|et al|vs|Fig|Eq|Sec|Dr|Mr|Ms|No|approx)\.$/i;
  function isBoundary(text, i) {
    const ch = text[i];
    if (ch === "\n" || "。！？!?".indexOf(ch) >= 0) return true;
    if (ch !== ".") return false;
    const next = text[i + 1] || "";
    if (/[A-Za-z0-9]/.test(next)) return false;
    const before = text.slice(Math.max(0, i - 12), i + 1);
    if (ABBR.test(before)) return false;
    if (/\b[A-Z]\.$/.test(before)) return false;
    return true;
  }

  function sentenceAt(text, index) {
    let start = index;
    while (start > 0 && !isBoundary(text, start - 1)) start--;
    let end = index;
    while (end < text.length && !isBoundary(text, end)) end++;
    if (end < text.length && /[。！？!?]/.test(text[end])) end++;
    let s = text.slice(start, end);
    // 句首常带列表符号/编号，去掉但保持 start 有效
    const lead = (s.match(/^\s*(?:[-*•]|\[\d+\]|\d+[.)])\s*/) || [""])[0].length;
    start += lead;
    s = text.slice(start, end);
    return { start: start, end: end, text: normWs(s) };
  }

  function lineOf(text, offset) {
    let n = 1;
    for (let i = 0; i < offset && i < text.length; i++) {
      if (text[i] === "\n") n++;
    }
    return n;
  }

  /**
   * 把一条引文钉回原文。
   * @returns {{level:"T1"|"T2"|"T3", quote:string, line:number|null, offset:number|null, reason:string}}
   */
  function anchorQuote(text, quote) {
    const q = normWs(quote);
    if (!q) {
      return { level: "T3", quote: "", line: null, offset: null, reason: "没有可核对的引文" };
    }
    const hit = findQuote(text, q);
    if (hit) {
      const line = lineOf(text, hit.start);
      if (normWs(hit.text).replace(/\s/g, "").length >= T1_MIN) {
        return { level: "T1", quote: normWs(hit.text), line: line, offset: hit.start,
          reason: "逐字命中原文（≥40 字符）" };
      }
      return { level: "T2", quote: normWs(hit.text), line: line, offset: hit.start,
        reason: "逐字命中，但短于 40 字符，只能算部分锚定" };
    }
    // 退化：取前 6 个词再找一次，命中则锚到整句（T2）
    const words = q.split(/\s+/).slice(0, 6).join(" ");
    const hit2 = words.length >= 6 ? findQuote(text, words) : null;
    if (hit2) {
      const sent = sentenceAt(text, hit2.start);
      return { level: "T2", quote: sent.text, line: lineOf(text, sent.start),
        offset: sent.start, reason: "整句未逐字命中，只匹配到开头片段" };
    }
    return { level: "T3", quote: "", line: null, offset: null,
      reason: "原文里找不到这段文字" };
  }

  /** 规则命中处 → 所在句；句子够长即 T1。 */
  function anchorAt(text, index, matchText) {
    const sent = sentenceAt(text, index);
    if (!sent.text) {
      return anchorQuote(text, matchText || "");
    }
    const line = lineOf(text, sent.start);
    if (sent.text.replace(/\s/g, "").length >= T1_MIN) {
      return { level: "T1", quote: sent.text, line: line, offset: sent.start,
        reason: "命中处所在句已逐字回原文" };
    }
    return { level: "T2", quote: sent.text, line: line, offset: sent.start,
      reason: "命中处所在句过短，标为部分锚定" };
  }

  /** 按章节标题找线索（用于「未检出」项的补证）；找不到即 T3。 */
  function anchorSection(text, hints) {
    const hs = (hints || []).map(function (x) { return x.toLowerCase(); });
    if (!hs.length) return { level: "T3", quote: "", line: null, offset: null,
      reason: "没有章节线索" };
    const lines = text.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i].trim();
      if (!l || l.length > 80) continue;
      const low = l.toLowerCase();
      if (hs.some(function (h) { return low.indexOf(h) >= 0; })) {
        const offset = lines.slice(0, i).join("\n").length + (i ? 1 : 0);
        return { level: "T2", quote: l, line: i + 1, offset: offset,
          reason: "找到相关章节标题，但未找到对应正文表述" };
      }
    }
    return { level: "T3", quote: "", line: null, offset: null,
      reason: "没有找到相关章节" };
  }

  /** 拆出正文与参考文献；参考文献只用于漏引分析，不参与规则扫描。 */
  function splitReferences(text) {
    const src = text || "";
    const m = REF_HEAD.exec(src);
    if (!m) {
      return { body: src, refsText: "", refs: [], found: false, headingLine: null };
    }
    const body = src.slice(0, m.index);
    const refsText = src.slice(m.index + m[0].length);
    return {
      body: body,
      refsText: refsText,
      refs: parseRefEntries(refsText),
      found: true,
      headingLine: lineOf(src, m.index)
    };
  }

  const VENUE_WORDS = /^(in |proceedings|advances|arxiv|journal|transactions|conference|workshop|pp\.|pages|vol\.|ieee|acm|springer|mit press|openreview)/i;
  const AUTHOR_HINT = /\b(university|department|institute|laboratory|inc\.|ltd\.|school of)\b/i;

  function guessTitle(entry) {
    let s = entry.replace(/^\s*\[\d+\]\s*/, "").replace(/^\s*\d+[.)]\s*/, "");
    s = s.replace(/https?:\/\/\S+/g, " ").replace(/\bdoi:\s*\S+/gi, " ")
      .replace(/\barxiv:\s*\S+/gi, " ").replace(/\s+/g, " ").trim();
    const quoted = s.match(/[“"]([^”"]{10,240})[”"]/);
    if (quoted) return normWs(quoted[1]);
    // 按句号切；参考文献里的标题通常是一个独立片段
    const parts = s.split(/(?<=[.!?])\s+(?=[A-Z“"(\u4e00-\u9fff])/);
    let best = "", bestScore = -1;
    parts.forEach(function (p) {
      const t = normWs(p).replace(/[.,;]+$/, "");
      if (t.length < 12 || t.length > 240) return;
      let score = 0;
      if (t.length >= 25 && t.length <= 180) score += 3;
      const caps = (t.match(/\b[A-Z][a-z]{2,}/g) || []).length;
      score += Math.min(caps, 6);
      if (VENUE_WORDS.test(t)) score -= 8;
      if (AUTHOR_HINT.test(t)) score -= 6;
      if ((t.match(/,/g) || []).length >= 3) score -= 3;
      if (/^\d{4}$/.test(t)) score -= 10;
      if (score > bestScore) { bestScore = score; best = t; }
    });
    if (best) return best;
    // 兜底：取最长的一段
    return normWs(s).slice(0, 240);
  }

  function parseRefEntries(refsText) {
    const txt = (refsText || "").trim();
    if (!txt) return [];
    let chunks = [];
    if (/^\s*\[\d+\]/m.test(txt)) {
      chunks = txt.split(/(?=^\s*\[\d+\])/m);
    } else if (/^\s*\d+[.)]\s+/m.test(txt)) {
      chunks = txt.split(/(?=^\s*\d+[.)]\s+)/m);
    } else if (/\n\s*\n/.test(txt)) {
      chunks = txt.split(/\n\s*\n/);
    } else {
      chunks = txt.split(/\n/);
    }
    const out = [], seen = new Set();
    chunks.forEach(function (c) {
      const entry = normWs(c);
      if (entry.length < 15) return;
      const title = guessTitle(entry).replace(/^[\s"'“”‘’「」]+|[\s"'“”‘’「」]+$/g, "");
      if (title.length < 10 || title.length > 300) return;
      const key = title.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      out.push(title);
    });
    return out.slice(0, 120);
  }

  /** 章节标题列表（供 UI 展示定位）。 */
  function headings(text) {
    const out = [];
    let m;
    HEADING.lastIndex = 0;
    while ((m = HEADING.exec(text || "")) !== null) {
      const t = normWs(m[1]);
      if (t && t.length >= 2 && t.length <= 70) out.push({ title: t, line: lineOf(text, m.index) });
      if (out.length > 60) break;
    }
    return out;
  }

  function stats(anchors) {
    const s = { T1: 0, T2: 0, T3: 0 };
    (anchors || []).forEach(function (a) {
      if (a && s[a.level] !== undefined) s[a.level]++;
    });
    return s;
  }

  root.QZ_ANCHOR = {
    T1_MIN: T1_MIN,
    normWs: normWs,
    anchorQuote: anchorQuote,
    anchorAt: anchorAt,
    anchorSection: anchorSection,
    splitReferences: splitReferences,
    parseRefEntries: parseRefEntries,
    guessTitle: guessTitle,
    headings: headings,
    stats: stats,
    lineOf: lineOf
  };
})(typeof window !== "undefined" ? window : globalThis);
