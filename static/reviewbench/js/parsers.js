/* 文献导入解析：BibTeX / RIS / CSV / JSON / 逐行速记
   统一产出条目结构，字段缺失时留空而不伪造。 */

export const TYPES = {
  journal: '期刊论文',
  conference: '会议论文',
  preprint: '预印本',
  book: '专著',
  thesis: '学位论文',
  web: '网页/在线',
  other: '其他',
};

const BIB_TYPE_MAP = [
  [/^(article|journalpaper)$/, 'journal'],
  [/^(inproceedings|conference|proceedings|incollection)$/, 'conference'],
  [/^(online|electronic|www|miscweb)$/, 'web'],
  [/^(book|booklet|monograph)$/, 'book'],
  [/^(phdthesis|mastersthesis|thesis)$/, 'thesis'],
  [/^(techreport|unpublished|manuscript)$/, 'preprint'],
];

const normType = (t) => {
  for (const [re, out] of BIB_TYPE_MAP) if (re.test(t)) return out;
  return 'other';
};

let seq = 0;
export const uid = () => `e${Date.now().toString(36)}${(seq++).toString(36)}${Math.random().toString(36).slice(2, 6)}`;

/** 空条目模板：所有面板共用同一形状，避免到处判空 */
export function blankItem(seed = {}) {
  return {
    id: seed.id || uid(),
    title: (seed.title || '').trim(),
    authors: Array.isArray(seed.authors) ? seed.authors.filter(Boolean) : seed.authors ? [seed.authors] : [],
    year: Number.isFinite(+seed.year) && +seed.year > 0 ? +seed.year : null,
    venue: (seed.venue || '').trim(),
    type: TYPES[seed.type] ? seed.type : 'other',
    doi: (seed.doi || '').trim(),
    url: (seed.url || '').trim(),
    abstract: (seed.abstract || '').trim(),
    keywords: Array.isArray(seed.keywords) ? seed.keywords.map((k) => String(k).trim()).filter(Boolean) : splitKw(seed.keywords),
    status: ['todo', 'reading', 'done'].includes(seed.status) ? seed.status : 'todo',
    matrix: {
      method: seed.matrix?.method || '',
      data: seed.matrix?.data || '',
      findings: seed.matrix?.findings || '',
      limits: seed.matrix?.limits || '',
    },
    notes: seed.notes || '',
    addedAt: seed.addedAt || Date.now(),
    demo: seed.demo === true,
  };
}

const splitKw = (s) => String(s || '').split(/[;，,、|]+/).map((x) => x.trim()).filter(Boolean);

/** "Doe, J. and Smith, R." → ["Jane Doe" 形式的人名]，供著录用 */
export function cleanAuthor(name) {
  const s = String(name).replace(/\s+/g, ' ').trim();
  if (!s) return '';
  if (!s.includes(',')) return s.replace(/\{([^{]*)\}/g, '$1');
  const [last, ...rest] = s.split(',');
  const given = rest.join(' ').trim();
  return given ? `${given} ${last}`.trim() : last.trim();
}

const stripBraces = (s) => String(s).replace(/[{}]/g, '').replace(/\\[a-zA-Z]{2,}\{([a-zA-Z])\}/g, '$1').replace(/\\&/g, '&').trim();

/* ---------------- BibTeX ---------------- */

function parseBibEntryBody(body) {
  const fields = {};
  let i = 0;
  const n = body.length;
  while (i < n) {
    while (i < n && /[\s,]/.test(body[i])) i++;
    const eq = body.indexOf('=', i);
    if (eq < 0) break;
    const key = body.slice(i, eq).trim().toLowerCase().replace(/[",]/g, '');
    let j = eq + 1;
    while (j < n && /\s/.test(body[j])) j++;
    let value = '';
    if (body[j] === '{' || body[j] === '(') {
      const open = body[j], close = open === '{' ? '}' : ')';
      let depth = 0, k = j;
      for (; k < n; k++) {
        if (body[k] === open && body[k - 1] !== '\\') depth++;
        else if (body[k] === close && body[k - 1] !== '\\') { depth--; if (!depth) break; }
      }
      value = body.slice(j + 1, Math.max(j, k));
      j = k + 1;
    } else if (body[j] === '"') {
      let k = j + 1;
      let depth = 0;
      for (; k < n; k++) {
        if (body[k] === '{') depth++;
        else if (body[k] === '}') depth--;
        else if (body[k] === '"' && !depth) break;
      }
      value = body.slice(j + 1, k);
      j = k + 1;
    } else {
      let k = j;
      while (k < n && body[k] !== ',') k++;
      value = body.slice(j, k);
      j = k;
    }
    if (key) fields[key] = value.trim();
    i = j;
  }
  return fields;
}

export function parseBibTeX(text) {
  const items = [];
  let i = 0;
  while (true) {
    const at = text.indexOf('@', i);
    if (at < 0) break;
    let j = at + 1;
    while (j < text.length && /[^\s{([]/.test(text[j])) j++;
    const type = text.slice(at + 1, j).trim().toLowerCase();
    while (j < text.length && /\s/.test(text[j])) j++;
    const open = text[j];
    if (!type || (open !== '{' && open !== '(')) { i = at + 1; continue; }
    const close = open === '{' ? '}' : ')';
    let depth = 0, k = j;
    for (; k < text.length; k++) {
      if (text[k] === open) depth++;
      else if (text[k] === close) { depth--; if (!depth) break; }
    }
    const body = text.slice(j + 1, k);
    i = k + 1;
    if (/^(comment|preamble|string)$/.test(type)) continue;

    // 跳过第一个逗号前的引用键
    let c = 0, d = 0, keyEnd = -1;
    for (; c < body.length; c++) {
      const ch = body[c];
      if (ch === '{') d++;
      else if (ch === '}') d--;
      else if (ch === ',' && !d) { keyEnd = c; break; }
    }
    const f = parseBibEntryBody(keyEnd < 0 ? body : body.slice(keyEnd + 1));
    const title = stripBraces(f.title || f.articletitle || '');
    if (!title && !f.author) continue;

    items.push(blankItem({
      title,
      authors: (f.author || f.editor || '').split(/\s+and\s+/i).map(cleanAuthor).filter(Boolean),
      year: (f.year || (f.date || '').slice(0, 4) || '').match(/\d{4}/)?.[0] || null,
      venue: stripBraces(f.journal || f.booktitle || f.series || f.howpublished || f.school || f.institution || ''),
      type: normType(type),
      doi: f.doi || '',
      url: f.url || '',
      abstract: stripBraces(f.abstract || ''),
      keywords: splitKw(stripBraces(f.keywords || '')),
    }));
  }
  return items;
}

/* ---------------- RIS ---------------- */

const RIS_MAP = {
  TY: 'type', AU: 'authors', A2: 'authors', DA: 'year', PY: 'year',
  TI: 'title', T1: 'title', JO: 'venue', JF: 'venue', T2: 'venue', PB: 'venue',
  AB: 'abstract', N2: 'abstract', KW: 'keywords', DO: 'doi', UR: 'url', L1: 'url', L2: 'url',
};

const RIS_TYPE = {
  JOUR: 'journal', CONF: 'conference', CPAPER: 'conference', ABST: 'preprint',
  BOOK: 'book', CHAP: 'book', THES: 'thesis', ELEC: 'web', WEBPAGE: 'web', RPRT: 'preprint',
};

export function parseRIS(text) {
  const items = [];
  let cur = null;
  for (const line of text.split(/\r?\n/)) {
    const m = /^([A-Z][A-Z0-9])\s+-\s?(.*)$/.exec(line);
    if (!m) continue;
    const [, tag, raw] = m;
    const val = raw.trim();
    if (tag === 'ER') { if (cur?.title) items.push(cur); cur = null; continue; }
    const field = RIS_MAP[tag];
    if (!field) continue;
    cur ||= {};
    if (field === 'authors') (cur.authors ||= []).push(cleanAuthor(val));
    else if (field === 'keywords') (cur.keywords ||= []).push(val);
    else if (field === 'type') cur.type = RIS_TYPE[val.toUpperCase()] || 'other';
    else if (field === 'year') cur.year = val.match(/\d{4}/)?.[0] || null;
    else if (!cur[field]) cur[field] = val;
  }
  if (cur?.title) items.push(cur);
  return items.map(blankItem);
}

/* ---------------- CSV / TSV ---------------- */

export function parseDelimited(text) {
  const delim = (text.split('\n')[0]?.match(/\t/) && (text.match(/,/g) || []).length < 2) ? '\t' : ',';
  const rows = [];
  let row = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { cell += '"'; i++; }
        else quoted = false;
      } else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === delim) { row.push(cell); cell = ''; }
    else if (ch === '\n') { row.push(cell); rows.push(row); row = []; cell = ''; }
    else if (ch !== '\r') cell += ch;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }

  const head = (rows.shift() || []).map((h) => h.trim().toLowerCase().replace(/^\ufeff/, ''));
  if (!head.length) return [];
  const pick = (...names) => {
    for (const n of names) {
      const idx = head.findIndex((h) => h === n || h.includes(n));
      if (idx >= 0) return idx;
    }
    return -1;
  };
  const col = {
    title: pick('title', '题名', '标题'),
    authors: pick('authors', 'author', '作者'),
    year: pick('year', '日期', '年'),
    venue: pick('journal', 'booktitle', 'venue', 'publication', '来源', '期刊'),
    abstract: pick('abstract', '摘要'),
    keywords: pick('keywords', 'keyword', '关键词'),
    doi: pick('doi'),
    url: pick('url', 'link', '网址'),
    status: pick('status', '状态'),
    method: pick('method', '方法'),
    data: pick('data', '数据'),
    findings: pick('finding', 'result', 'conclusion', '结论'),
    limits: pick('limit', 'gap', 'future', '局限'),
  };
  const at = (r, i) => (i >= 0 ? (r[i] ?? '').trim() : '');

  return rows.filter((r) => r.some((x) => x.trim())).map((r) => blankItem({
    title: at(r, col.title),
    authors: splitKw(at(r, col.authors)),
    year: at(r, col.year).match(/\d{4}/)?.[0] || null,
    venue: at(r, col.venue),
    abstract: at(r, col.abstract),
    keywords: splitKw(at(r, col.keywords)),
    doi: at(r, col.doi),
    url: at(r, col.url),
    status: ({ 已读: 'done', 读完: 'done', done: 'done', 在读: 'reading', 未读: 'todo', todo: 'todo', reading: 'reading' })[at(r, col.status).toLowerCase()] || 'todo',
    matrix: { method: at(r, col.method), data: at(r, col.data), findings: at(r, col.findings), limits: at(r, col.limits) },
  }));
}

/* ---------------- 逐行速记 ---------------- */
/* 极简模式：一行一篇，尽力识别标题与 (年份)，其余字段留空 */
export function parseLines(text) {
  return text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l.length > 4).map((line) => {
    const year = line.match(/\((\d{4})\)/)?.[1] || null;
    const title = line
      .replace(/\((\d{4})\)/, ' ')
      .replace(/\s{2,}/g, ' ')
      .replace(/[.;,]+$/, '')
      .trim();
    return blankItem({ title: title.slice(0, 400), year });
  }).filter((it) => it.title);
}

/* ---------------- 统一入口 ---------------- */

export function autoParse(text) {
  const t = String(text).replace(/^\ufeff/, '');
  const trimmed = t.trim();
  if (!trimmed) return { items: [], format: 'empty' };
  if (trimmed[0] === '[' || trimmed[0] === '{') {
    try {
      const data = JSON.parse(trimmed);
      const arr = Array.isArray(data) ? data : data.items;
      if (Array.isArray(arr)) return { items: arr.map(blankItem), format: 'json' };
    } catch { /* 继续按其他格式尝试 */ }
  }
  if (/@\w+\s*[({]/.test(trimmed)) return { items: parseBibTeX(trimmed), format: 'bibtex' };
  if (/^[A-Z][A-Z0-9]\s+-\s/m.test(trimmed) && /\bTY\s+-/.test(trimmed)) return { items: parseRIS(trimmed), format: 'ris' };
  const firstLine = trimmed.split(/\r?\n/)[0];
  if (/[,;\t]/.test(firstLine) && /(title|标题|author|作者|ti\b)/i.test(firstLine)) return { items: parseDelimited(trimmed), format: 'csv' };
  return { items: parseLines(trimmed), format: 'lines' };
}

/** 合并导入：同 DOI 或同标题（归一化后）视为已有条目，只补空字段 */
export function mergeInto(existing, incoming) {
  const key = (it) => (it.doi || '').toLowerCase().replace(/^https?:\/\/(dx\.)?doi\.org\//, '');
  const normTitle = (it) => it.title.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  const index = new Map();
  existing.forEach((it) => {
    if (key(it)) index.set(`d:${key(it)}`, it);
    index.set(`t:${normTitle(it)}`, it);
  });
  let added = 0, updated = 0;
  const out = existing.slice();
  for (const inc of incoming) {
    const hit = index.get(`d:${key(inc)}`) || index.get(`t:${normTitle(inc)}`);
    if (hit) {
      let touched = false;
      for (const f of ['abstract', 'doi', 'url', 'venue', 'year']) {
        if (!hit[f] && inc[f]) { hit[f] = inc[f]; touched = true; }
      }
      if (!hit.keywords.length && inc.keywords.length) { hit.keywords = inc.keywords; touched = true; }
      if (!hit.authors.length && inc.authors.length) { hit.authors = inc.authors; touched = true; }
      if (!hit.title && inc.title) { hit.title = inc.title; touched = true; }
      if (touched) updated++;
    } else {
      const item = blankItem(inc);
      out.push(item);
      index.set(`d:${key(item)}`, item);
      index.set(`t:${normTitle(item)}`, item);
      added++;
    }
  }
  return { items: out, added, updated };
}
