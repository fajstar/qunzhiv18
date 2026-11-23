/* 综述台 前端主程序：状态、渲染、交互与本地持久化 */
import { blankItem, autoParse, mergeInto, TYPES } from './parsers.js';
import { cluster, findDuplicates, healthCheck, tokenize, yearHistogram } from './analysis.js';
import { buildDraft, FORMATTERS, formatGBT, formatAPA, formatBibTeX, toCSV } from './cite.js';
import { demoItems } from './demo.js';

const TYPE_LABEL = TYPES;
const LS = 'reviewbench.v1';
const STATUS = { todo: '未读', reading: '在读', done: '已读' };

const ICON = {
  search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5" stroke-linecap="round"/></svg>',
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4.2"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.5 1.5M16.9 16.9l1.5 1.5M18.4 5.6l-1.5 1.5M7.1 16.9l-1.5 1.5" stroke-linecap="round"/></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 14.5A8 8 0 019.5 4a7.5 7.5 0 1010.5 10.5z" stroke-linejoin="round"/></svg>',
};

const state = {
  items: [],
  selectedId: null,
  tab: 'detail',
  q: '',
  status: 'all',
  yearFrom: '',
  sort: 'year-asc',
  clusterFilter: null,
  k: 5,
  theme: 'light',
  analysis: { clusters: [], noise: [], topTerms: [] },
  draftDirty: false,
  draft: {
    title: '文献综述草稿',
    format: 'gbt',
    onlyDone: true,
    intro: true, gaps: true, refs: true, noise: true, bridge: true,
    clusterIds: null,
  },
};

/* ---------------- 工具 ---------------- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const callOf = (id) => state.items.findIndex((i) => i.id === id) + 1;
const debounce = (fn, ms = 260) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function toast(msg, kind = '', action) {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.innerHTML = `<span>${esc(msg)}</span>`;
  if (action) {
    const b = document.createElement('button');
    b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.append(b);
  }
  box.append(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 260); }, action ? 6500 : 3200);
}

/* ---------------- 持久化 ---------------- */
function save() {
  try {
    // QBUG-017：合并写，保留 live.js 写入的 source/week，避免整份覆盖。
    let prev = {};
    try { prev = JSON.parse(localStorage.getItem(LS) || 'null') || {}; } catch (e) { prev = {}; }
    const out = Object.assign({}, prev, {
      items: state.items, k: state.k, theme: state.theme,
      week: state.week || prev.week || '',
      draft: state.draft, sort: state.sort, selectedId: state.selectedId,
    });
    localStorage.setItem(LS, JSON.stringify(out));
  } catch (e) {
    toast('本地存储空间不足，本次修改未保存', 'bad');
  }
}

function load() {
  let data = null;
  try { data = JSON.parse(localStorage.getItem(LS) || 'null'); } catch { data = null; }
  if (!data) return false;
  state.items = (data.items || []).map((i) => blankItem(i));
  state.k = data.k || 5;
  state.theme = data.theme || 'light';
  state.sort = data.sort || 'year-asc';
  state.selectedId = data.selectedId || state.items[0]?.id || null;
  if (data.draft) Object.assign(state.draft, data.draft);
  return state.items.length > 0;
}

/* ---------------- 派生数据 ---------------- */
const reanalyze = debounce(() => {
  state.analysis = state.items.length > 1 ? cluster(state.items, state.k) : { clusters: [], noise: state.items.map((i) => i.id), topTerms: [] };
  renderMap();
  renderTally();
  renderList();
  syncDetailBadge();
  if (state.tab === 'draft') renderDraft();
}, 180);

/** 聚类完成后单独补一次精读页的主题角标：整块重渲染会把正在输入的字段焦点打掉 */
function syncDetailBadge() {
  const el = $('#detail-cluster');
  if (!el) return;
  const c = state.analysis.clusters?.find((x) => x.members.includes(state.selectedId));
  el.textContent = c ? `#${c.index} ${c.label}` : '未归类';
}

function filtered() {
  const q = state.q.trim().toLowerCase();
  const toks = q ? tokenize(q) : [];
  const yearFrom = +state.yearFrom || 0;
  const inCluster = state.clusterFilter == null ? null
    : new Set(state.clusterFilter === 'noise'
      ? state.analysis.noise
      : (state.analysis.clusters.find((c) => c.index === state.clusterFilter)?.members || []));

  let list = state.items.filter((it) => {
    if (state.status === 'unfilled') {
      const m = it.matrix;
      if (m.method && m.findings) return false;
    } else if (state.status !== 'all' && it.status !== state.status) return false;
    if (yearFrom && (!it.year || it.year < yearFrom)) return false;
    if (inCluster && !inCluster.has(it.id)) return false;
    if (!q) return true;
    const hay = `${it.title} ${it.authors.join(' ')} ${(it.keywords || []).join(' ')} ${it.abstract} ${it.venue}`.toLowerCase();
    if (hay.includes(q)) return true;
    // 词元匹配：让「评分」也能命中 essay scoring 之类的中文摘要分词结果
    return toks.some((t) => hay.includes(t));
  });

  const score = (it) => {
    if (!toks.length) return 0;
    const hay = `${it.title} ${(it.keywords || []).join(' ')} ${it.authors.join(' ')} ${it.abstract}`.toLowerCase();
    return toks.reduce((acc, t) => acc + (hay.includes(t) ? 1 : 0), 0) / toks.length;
  };

  const cmp = {
    'year-desc': (a, b) => (b.year || 0) - (a.year || 0) || a.title.localeCompare(b.title),
    'year-asc': (a, b) => (a.year || 9999) - (b.year || 9999) || a.title.localeCompare(b.title),
    title: (a, b) => a.title.localeCompare(b.title, 'zh'),
    relevance: (a, b) => score(b) - score(a) || (b.year || 0) - (a.year || 0),
    unread: (a, b) => (a.status === 'done') - (b.status === 'done') || (a.year || 0) - (b.year || 0),
  }[state.sort] || (() => 0);

  if (state.sort === 'relevance' && toks.length) list = list.filter((it) => score(it) > 0);
  return list.sort(cmp);
}

function statusCounts() {
  const c = { all: state.items.length, todo: 0, reading: 0, done: 0, unfilled: 0 };
  state.items.forEach((it) => {
    c[it.status]++;
    if (!(it.matrix.method && it.matrix.findings)) c.unfilled++;
  });
  return c;
}

/* ---------------- 顶栏 / 左栏 ---------------- */
function renderTally() {
  const n = state.items.length;
  const a = state.analysis;
  const done = state.items.filter((i) => i.status === 'done').length;
  const demo = state.items.filter((i) => i.demo).length;
  const parts = n
    ? [`共 <b>${n}</b> 篇`, `已读 <b>${done}</b>`, `主题簇 <b>${a.clusters?.length || 0}</b>`, `未归类 <b>${(a.noise || []).length}</b>`]
    : ['文献库为空'];
  if (demo && demo === n) parts.push('<b style="color:var(--raw-warn)">演示数据（虚构条目）</b>');
  else if (demo) parts.push(`其中 <b>${demo}</b> 篇来自示例库`);
  $('#tally').innerHTML = parts.join(' · ');
}

function renderChips() {
  const c = statusCounts();
  const defs = [['all', '全部'], ['todo', '未读'], ['reading', '在读'], ['done', '已读'], ['unfilled', '待填矩阵']];
  $('#status-chips').innerHTML = defs.map(([v, l]) =>
    `<button class="chip" type="button" data-status="${v}" aria-pressed="${state.status === v}">${l}<b>${c[v]}</b></button>`).join('');
}

function renderFilterNote() {
  const list = filtered();
  const notes = [];
  if (state.q) notes.push(`匹配「${esc(state.q)}」`);
  if (state.status !== 'all') notes.push(`仅${{ unfilled: '待填矩阵', ...STATUS }[state.status]}`);
  if (state.yearFrom) notes.push(`${esc(state.yearFrom)} 年及以后`);
  if (state.clusterFilter != null) {
    const name = state.clusterFilter === 'noise' ? '未归类' : state.analysis.clusters.find((c) => c.index === state.clusterFilter)?.label || '主题';
    notes.push(`主题「${esc(name)}」`);
  }
  const el = $('#filter-note');
  if (!notes.length) { el.innerHTML = state.items.length ? `显示全部 <b>${list.length}</b> 篇` : ''; return; }
  el.innerHTML = `${notes.join(' · ')} → <b>${list.length}</b> 篇 <button type="button" data-clear>清除筛选</button>`;
}

function renderList() {
  renderChips();
  renderFilterNote();
  const list = filtered();
  const box = $('#list');
  if (!list.length) {
    box.innerHTML = `<div class="list-empty">${state.items.length ? '没有符合筛选条件的文献。<br>换个关键词，或清除筛选。' : '还没有文献。<br>到「数据与体检」导入你的 .bib，或点顶栏载入示例库。'}</div>`;
    return;
  }
  box.innerHTML = list.map((it) => {
    const cl = state.analysis.clusters?.find((c) => c.members.includes(it.id));
    return `<button class="card" type="button" role="option" data-id="${it.id}" data-call="${callOf(it.id)}"
      aria-selected="${it.id === state.selectedId}">
      <span>
        <span class="card-title">${esc(it.title)}</span>
        <span class="card-meta">
          <span class="dot s-${it.status}" aria-hidden="true"></span>
          <i>${it.year || '—'}</i>
          <span>${esc(it.authors[0] ? it.authors[0].split(' ').at(-1) : '佚名')}${it.authors.length > 1 ? ' 等' : ''}</span>
          ${cl ? `<span class="tag-mini">#${cl.index} ${esc(cl.terms[0]?.term || cl.label)}</span>` : ''}
        </span>
      </span>
    </button>`;
  }).join('');

  const pct = state.items.length ? state.items.filter((i) => i.status === 'done').length / state.items.length : 0;
  $('#meter-fill').style.width = `${Math.round(pct * 100)}%`;
  $('#meter-label').textContent = `精读进度 ${Math.round(pct * 100)}% ｜ 当前视图 ${list.length} 篇`;
}

/* ---------------- 单篇精读 ---------------- */
const MATRIX_FIELDS = [['method', '方法 / 技术路线', '它做了什么，怎么做的'], ['data', '数据 / 场景', '样本、被试、领域'], ['findings', '主要结论', '报告了什么效果'], ['limits', '局限 / 作者自陈缺口', '没解决什么、能否复现']];

function renderDetail() {
  const it = state.items.find((i) => i.id === state.selectedId) || filtered()[0];
  const body = $('#detail-body');
  if (!it) {
    body.innerHTML = `<div class="empty"><h3>还没有可精读的条目</h3><p class="hint">左侧列表选一篇开始，或先导入文献。</p><button class="btn btn-primary" data-act="demo" type="button">载入示例库</button></div>`;
    return;
  }
  state.selectedId = it.id;
  const n = callOf(it.id);
  const kw = (it.keywords || []).map((k) => `<span class="tag">${esc(k)}<button type="button" data-kw-del="${esc(k)}" aria-label="删除标签 ${esc(k)}">✕</button></span>`).join('');
  const clusterOf = state.analysis.clusters?.find((c) => c.members.includes(it.id));

  body.innerHTML = `
    <div class="detail-head">
      <h2 class="detail-title">${esc(it.title)}</h2>
      <div class="detail-line">
        <span class="badge accent">[${n}]</span>
        ${clusterOf ? `<span class="badge" id="detail-cluster">#${clusterOf.index} ${esc(clusterOf.label)}</span>` : '<span class="badge" id="detail-cluster">未归类</span>'}
        ${it.demo ? '<span class="badge warn">演示条目</span>' : ''}
        <span>${esc(it.authors.join('; ') || '作者待补')}</span>
        <span>·</span><span>${it.year || '年份待补'}</span>
        <span>·</span><span>${esc(TYPE_LABEL[it.type])}</span>
        ${it.doi ? `<a href="https://doi.org/${esc(it.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//, ''))}" target="_blank" rel="noopener">doi:${esc(it.doi)}</a>` : ''}
        ${!it.doi && it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">原文链接</a>` : ''}
      </div>
      <div class="detail-line">
        <span class="status-seg" role="group" aria-label="阅读状态">
          ${Object.entries(STATUS).map(([v, l]) => `<button type="button" data-status-set="${v}" aria-pressed="${it.status === v}">${l}</button>`).join('')}
        </span>
        <span class="muted" style="font-size:var(--fs-xs)">标记「已读」后，这篇才会在综述草稿里写成具体论断。</span>
      </div>
    </div>

    <div class="section-title"><h3>元数据</h3><span>改完即存</span></div>
    <div class="grid-2">
      <div class="field-block"><label for="f-title">标题</label><textarea id="f-title" data-field="title" rows="2">${esc(it.title)}</textarea></div>
      <div class="field-block"><label for="f-authors">作者（分号或 and 分隔）</label><textarea id="f-authors" data-field="authors" rows="2">${esc(it.authors.join('; '))}</textarea></div>
      <div class="field-block"><label for="f-venue">来源 / 期刊 / 会议</label><input id="f-venue" data-field="venue" value="${esc(it.venue)}"></div>
      <div class="field-block"><label for="f-year">年份</label><input id="f-year" data-field="year" type="number" min="1500" max="2100" value="${it.year || ''}"></div>
      <div class="field-block"><label for="f-type">类型</label>
        <select id="f-type" data-field="type">${Object.entries(TYPES).map(([v, l]) => `<option value="${v}"${it.type === v ? ' selected' : ''}>${l}</option>`).join('')}</select>
      </div>
      <div class="field-block"><label for="f-doi">DOI</label><input id="f-doi" data-field="doi" value="${esc(it.doi)}" placeholder="10.xxxx/…（可留空）"></div>
      <div class="field-block" style="grid-column:1/-1"><label for="f-kw">关键词标签 <span class="muted">回车添加，用于聚类加权</span></label>
        <div class="tag-editor" id="kw-editor">${kw || '<span class="muted" style="font-size:var(--fs-xs)">暂无标签</span>'}
          <input class="tag-input" id="f-kw" placeholder="添加标签…"></div>
      </div>
    </div>

    <div class="section-title"><h3>摘要</h3><span>${(it.abstract || '').length} 字</span></div>
    <div class="field-block abstract"><textarea id="f-abstract" data-field="abstract" placeholder="把论文摘要粘进来，主题聚类会用到它">${esc(it.abstract)}</textarea></div>

    <div class="section-title"><h3>精读矩阵</h3><span>这四项决定综述草稿能写多实</span></div>
    <div class="grid-2">
      ${MATRIX_FIELDS.map(([f, l, p]) => `<div class="field-block matrix-cell">
        <label for="m-${f}">${l} <em>${it.matrix[f] ? '已填' : '待填'}</em></label>
        <textarea id="m-${f}" data-matrix="${f}" rows="3" placeholder="${p}">${esc(it.matrix[f])}</textarea>
      </div>`).join('')}
    </div>

    <div class="section-title"><h3>我的笔记</h3></div>
    <div class="field-block"><textarea id="f-notes" data-field="notes" rows="3" placeholder="读的时候想到的、要追问作者的问题">${esc(it.notes)}</textarea></div>

    <div class="section-title"><h3>著录预览</h3><span>库内定位号 [${n}]；草稿里的 [n] 按引用先后重排</span></div>
    <div class="cite-preview" id="cite-gbt">${esc(formatGBT(it))}</div>
    <p style="height:var(--s-4)"></p>
    <div class="cite-preview" id="cite-apa">${esc(formatAPA(it))}</div>
    <div class="file-row">
      <button class="btn btn-sm" data-act="copy-cite" type="button">复制 GB/T 7714 条目</button>
      <button class="btn btn-sm" data-act="copy-bib" type="button">复制 BibTeX</button>
      <button class="btn btn-sm" data-act="prev" ${n <= 1 ? 'disabled' : ''} type="button">← 上一篇</button>
      <button class="btn btn-sm" data-act="next" ${n >= state.items.length ? 'disabled' : ''} type="button">下一篇 →</button>
      <button class="btn btn-sm" data-act="del" type="button" style="margin-inline-start:auto">删除此条</button>
    </div>`;
}

/* ---------------- 主题图谱 ---------------- */
function renderMap() {
  const body = $('#map-body');
  const { clusters = [], noise = [], topTerms = [] } = state.analysis;
  if (state.items.length < 2) {
    body.innerHTML = `<div class="empty"><h3>至少需要 2 篇带文字的材料</h3><p class="hint">聚类依靠标题、关键词和摘要的 TF-IDF 相似度。导入文献或载入示例库后，这里会自动出图。</p><button class="btn btn-primary" data-act="demo" type="button">载入示例库</button></div>`;
    return;
  }
  const hist = yearHistogram(state.items);
  const maxBucket = Math.max(1, ...hist.buckets.map((b) => b.n));
  const byId = new Map(state.items.map((i) => [i.id, i]));

  const spark = (members) => {
    const counts = hist.buckets.map((b) => members.reduce((acc, id) => acc + (byId.get(id)?.year === b.year ? 1 : 0), 0));
    const m = Math.max(1, ...counts);
    return `<div class="spark" aria-hidden="true">${counts.map((c) => `<i class="${c ? '' : 'zero'}" style="height:${c ? Math.max(8, (c / m) * 100) : 6}%"></i>`).join('')}</div>
      <div class="spark-x"><span>${hist.from}</span><span>本簇年份分布</span><span>${hist.to}</span></div>`;
  };

  const card = (c, isNoise = false) => `
    <article class="cluster${isNoise ? ' cluster-noise' : ''}">
      <div class="cluster-top">
        <h3 class="cluster-name">${isNoise ? '未归类' : esc(c.label)}</h3>
        <div class="cluster-count">${c.members.length}<small>${isNoise ? '篇' : `#${c.index} 簇`}</small></div>
      </div>
      ${isNoise ? '<p class="hint" style="font-size:var(--fs-xs)">与所有主题中心的余弦相似度都低于阈值。可能是边缘材料，也可能提示缺一整个主题。</p>' : ''}
      ${(c.terms || []).length ? `<div class="terms">${c.terms.map((t, i) => `<span class="term${i < 2 ? ' hi' : ''}">${esc(t.term)} ${t.docs}</span>`).join('')}</div>` : ''}
      ${spark(c.members)}
      <div class="cluster-items">
        ${c.members.slice(0, 6).map((id) => {
          const it = byId.get(id);
          return it ? `<button class="cluster-item" type="button" data-open="${it.id}"><code>[${callOf(it.id)}]</code><span>${esc(it.title)}</span></button>` : '';
        }).join('')}
        ${c.members.length > 6 ? `<span class="hint" style="font-size:var(--fs-xs)">另有 ${c.members.length - 6} 篇，点右下角「只看这簇」</span>` : ''}
      </div>
      <div class="cluster-foot">
        <span>已读 ${c.done ?? c.members.filter((id) => byId.get(id)?.status === 'done').length}</span>
        <span class="progress"><i style="width:${c.members.length ? Math.round(((c.done ?? 0) / c.members.length) * 100) : 0}%"></i></span>
        <button class="btn btn-ghost btn-sm" type="button" data-cluster-filter="${isNoise ? 'noise' : c.index}">只看这簇</button>
      </div>
    </article>`;

  body.innerHTML = `
    <div class="map-summary">
      <span>主题簇 <b>${clusters.length}</b></span>
      <span>覆盖 <b>${clusters.reduce((a, c) => a + c.members.length, 0)}</b>/${state.items.length} 篇</span>
      <span>未归类 <b>${noise.length}</b></span>
      <span>算法 <b>中英分词 + TF-IDF + k-medoids（k=${state.k}，相似度阈值 ${state.analysis.threshold ?? 0.02}）</b></span>
    </div>
    <div class="clusters">
      ${clusters.map((c) => card(c)).join('')}
      ${noise.length ? card({ members: noise, terms: [], done: noise.filter((id) => byId.get(id)?.status === 'done').length }, true) : ''}
    </div>
    <div class="section-title"><h3>高频主题词</h3><span>文档频次 df × 逆文档频率 idf</span></div>
    <div class="terms">${topTerms.slice(0, 18).map((t) => `<span class="term">${esc(t.term)} <b>df${t.df}</b></span>`).join('')}</div>
    <p class="hint" style="margin-top:var(--s-6)">簇数是可调的：把 k 拉大能拆出更细的路线差异，拉小则合并成几大方向。簇内条目顺序按年份，方便看出技术路线的演进。</p>`;
}

/* ---------------- 对比矩阵 ---------------- */
function renderMatrix() {
  const body = $('#matrix-body');
  const list = filtered();
  if (!list.length) {
    body.innerHTML = `<div class="empty"><h3>当前视图没有文献</h3><p class="hint">清除左侧筛选，或先导入文献。</p></div>`;
    return;
  }
  body.innerHTML = `
    <div class="table-wrap">
      <table class="matrix">
        <caption class="sr-only">按当前筛选显示的精读对比矩阵，可直接编辑</caption>
        <thead><tr>
          <th scope="col">编号 / 文献</th>
          ${MATRIX_FIELDS.map(([f, l]) => `<th scope="col">${l}<small>${f}</small></th>`).join('')}
          <th scope="col">状态</th>
        </tr></thead>
        <tbody>
          ${list.map((it) => `<tr class="${it.id === state.selectedId ? 'sel' : ''}" data-row="${it.id}">
            <td class="col-title">
              <span class="rowno">[${callOf(it.id)}]</span>
              <button class="rowtitle" type="button" data-open="${it.id}">${esc(it.title)}</button>
              <span class="rowmeta">${esc((it.authors[0] || '佚名').split(' ').at(-1))} · ${it.year || '—'} · ${esc(it.venue || TYPE_LABEL[it.type])}</span>
            </td>
            ${MATRIX_FIELDS.map(([f, l]) => `<td class="col-cell"><textarea class="cell-edit ${it.matrix[f] ? '' : 'empty'}" data-matrix="${f}" data-id="${it.id}" rows="2" placeholder="待填">${esc(it.matrix[f])}</textarea></td>`).join('')}
            <td><span class="badge ${it.status === 'done' ? 'ok' : it.status === 'reading' ? 'warn' : ''}">${STATUS[it.status]}</span></td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>
    <p class="hint" style="margin-top:var(--s-5)">单元格直接改，失焦即存。想只看某一簇或某几年的材料，先用左侧筛选——矩阵、综述草稿、导出 CSV 都遵循同一份筛选结果。</p>`;
}

/* ---------------- 综述草稿 ---------------- */
function renderDraftControls() {
  const { clusters = [] } = state.analysis;
  const d = state.draft;
  const picked = d.clusterIds || clusters.map((c) => c.index);
  $('#draft-controls').innerHTML = `
    <div class="field-block"><label for="d-title">综述标题</label><input id="d-title" data-draft="title" value="${esc(d.title)}"></div>

    <h4>著录格式</h4>
    <div class="radio-list">
      ${Object.entries(FORMATTERS).map(([v, o]) => `<label><input type="radio" name="fmt" data-draft="format" value="${v}" ${d.format === v ? 'checked' : ''}>${o.label}</label>`).join('')}
    </div>

    <h4>纳入哪些主题簇</h4>
    ${clusters.length ? `<div class="check-list">
      ${clusters.map((c) => `<label class="${picked.includes(c.index) ? '' : 'dim'}"><input type="checkbox" data-cluster-pick="${c.index}" ${picked.includes(c.index) ? 'checked' : ''}>
        <span>#${c.index} ${esc(c.label)}</span><span class="count">${c.members.length} 篇</span></label>`).join('')}
    </div>` : '<p class="hint">还没有聚类结果。</p>'}

    <h4>写作选项</h4>
    <div class="check-list">
      ${[['onlyDone', '只为已读的文献写具体论断'], ['intro', '生成引言骨架'], ['bridge', '每节后插承接句提示'], ['gaps', '生成研究空白一节'], ['noise', '单列未归类文献'], ['refs', '末尾附参考文献表']]
        .map(([k, l]) => `<label><input type="checkbox" data-draft="${k}" ${d[k] ? 'checked' : ''}><span>${l}</span></label>`).join('')}
    </div>
    <button class="btn" id="btn-regen" type="button">重新生成</button>
    <p class="hint" style="font-size:var(--fs-xs)">右侧文本框可以直接改；你改过之后不会被自动覆盖，需要更新时点「重新生成」。</p>`;
}

function generateDraft() {
  const { clusters, noise, topTerms } = state.analysis;
  const res = buildDraft({
    items: state.items,
    clusters: clusters || [],
    noise: noise || [],
    topTerms: topTerms || [],
    opts: { ...state.draft, clusterIds: state.draft.clusterIds || undefined },
    title: state.draft.title,
  });
  $('#draft-text').value = res.markdown;
  $('#draft-stat').textContent = `${res.sections} 个小节 ｜ ${res.made} 条具体论断来自你的矩阵 ｜ ${res.pending} 处待补 ｜ 引用 ${res.cited} 篇 ｜ ${(res.markdown.match(/\S+/g) || []).length} 个词块`;
  state.draftDirty = false;
  return res;
}

function renderDraft() {
  renderDraftControls();
  if (state.items.length < 2) {
    $('#draft-text').value = '';
    $('#draft-stat').textContent = '—';
    $('#draft-controls').innerHTML = '<p class="hint">先导入文献或载入示例库。综述骨架由主题簇 + 精读矩阵拼成，两者都还需要材料。</p>';
    return;
  }
  if (!state.draftDirty) generateDraft();
}

/* ---------------- 数据与体检 ---------------- */
const IMPORT_PLACEHOLDER = {
  bibtex: `@article{halloran2025,\n  author = {Halloran, M. and Nascimento, P.},\n  title  = {Large language models as reliable graders?},\n  journal= {Journal of Learning Analytics},\n  year   = {2025},\n  doi    = {10.xxxx/yyyy}\n}`,
  ris: `TY  - JOUR\nAU  - Halloran, M.\nTI  - Large language models as reliable graders?\nPY  - 2025\nJO  - Journal of Learning Analytics\nER  -`,
  csv: `title,authors,year,venue,abstract,keywords,status,method,findings,limits`,
  json: `{"items":[{"title":"…","authors":["…"],"year":2025,"status":"done","matrix":{"method":"…"}}]}`,
  lines: `一篇一行，只识别标题与 (年份)：\nLarge language models as reliable graders? (2025)\nRubric-aware prompting improves transfer (2026)`,
};
let importKind = 'bibtex';

function renderData() {
  const checks = healthCheck(state.items);
  const dups = state.items.length > 1 ? findDuplicates(state.items) : [];
  $('#data-body').innerHTML = `
    <div class="grid-2">
      <div class="card-box">
        <h3>导入</h3>
        <p class="hint" style="margin:var(--s-3) 0 var(--s-5)">从 Zotero / CNKI / Web of Science 导出对应格式，整段粘进来即可。同 DOI 或同标题的条目会被合并，不会重复入库。</p>
        <div class="import-tabs" role="group" aria-label="导入格式">
          ${Object.keys(IMPORT_PLACEHOLDER).map((k) => `<button class="chip" type="button" data-import="${k}" aria-pressed="${importKind === k}">${{ bibtex: 'BibTeX', ris: 'RIS / EndNote', csv: 'CSV / TSV', json: 'JSON 备份', lines: '逐行速记' }[k]}</button>`).join('')}
        </div>
        <textarea class="raw-in" id="raw-in" spellcheck="false" placeholder="${esc(IMPORT_PLACEHOLDER[importKind])}" aria-label="粘贴${importKind}文本"></textarea>
        <div class="file-row">
          <button class="btn btn-primary" data-act="parse" type="button">解析并加入文献库</button>
          <label class="btn">选择文件<input type="file" id="file-in" accept=".bib,.ris,.txt,.csv,.tsv,.json" multiple></label>
          <button class="btn btn-ghost" data-act="replace-demo" type="button">用示例库替换当前库</button>
        </div>
        <p class="drop-hint">也可以把 .bib / .ris / .csv / .json 文件直接拖到页面上。</p>
      </div>

      <div class="card-box">
        <h3>手工补录 / 备份导出</h3>
        <div class="grid-2" style="margin-top:var(--s-4)">
          <div class="field-block"><label for="n-title">标题</label><input id="n-title" placeholder="必填"></div>
          <div class="field-block"><label for="n-year">年份</label><input id="n-year" type="number" min="1500" max="2100"></div>
          <div class="field-block"><label for="n-authors">作者</label><input id="n-authors" placeholder="分号分隔"></div>
          <div class="field-block"><label for="n-venue">来源</label><input id="n-venue" placeholder="期刊 / 会议"></div>
        </div>
        <div class="file-row"><button class="btn" data-act="add" type="button">添加一条</button></div>
        <hr style="border:0;border-top:1px solid var(--site-border);margin:var(--s-6) 0">
        <h3 style="font-size:var(--fs-h4)">带走你的数据</h3>
        <div class="file-row">
          <button class="btn" data-act="exp-json" type="button">JSON 备份（含精读矩阵）</button>
          <button class="btn" data-act="exp-bib" type="button">BibTeX</button>
          <button class="btn" data-act="exp-csv" type="button">CSV</button>
          <button class="btn btn-ghost" data-act="wipe" type="button" style="margin-inline-start:auto">清空文献库</button>
        </div>
        <p class="hint" style="margin-top:var(--s-5)">全部计算与存储都在你当前浏览器的 localStorage 里完成：没有账号、没有服务器、不会上传任何文献内容。换电脑请用上面的 JSON 备份带走。</p>
      </div>
    </div>

    <div class="section-title"><h3>文献库体检</h3><span>综述能不能写下去，先看这几项</span></div>
    ${state.items.length ? `<div class="checks">
      ${checks.map((c) => `<div class="check is-${c.level}">
        <div><h4>${c.label}</h4><p>${esc(c.detail)}</p></div>
        <div class="check-side"><b>${esc(String(c.value))}</b><span class="badge ${c.level}">${{ ok: '达标', warn: '注意', bad: '告急' }[c.level]}</span></div>
      </div>`).join('')}
    </div>` : '<p class="hint">文献库为空，暂无可体检内容。</p>'}

    <div class="section-title"><h3>疑似重复录入</h3><span>标题词集 Jaccard + 同年 / 同第一作者加成</span></div>
    ${dups.length ? `<p class="hint" style="margin-bottom:var(--s-4)">合并时保留精读信息更足的那条，摘要、DOI、链接、关键词与空着的矩阵格会从另一条补过来。</p><div class="card-box">${dups.map((p) => `
      <div class="dup-pair">
        <div class="dup-line"><code>[${callOf(p.a.id)}]</code><span>${esc(p.a.title)}</span></div>
        <div class="dup-line"><code>[${callOf(p.b.id)}]</code><span>${esc(p.b.title)}</span></div>
        <div class="dup-act">
          <span class="badge warn">相似度 ${Math.round(p.sim * 100)}%</span>
          <span class="simbar"><i style="width:${Math.round(p.sim * 100)}%"></i></span>
          <button class="btn btn-sm" data-act="merge" data-a="${p.a.id}" data-b="${p.b.id}" type="button">合并两条</button>
        </div>
      </div>`).join('')}</div>` : '<p class="hint">没有发现可疑重复。预印本与正式发表版是最常见的重复来源。</p>'}`;
}

/* ---------------- 面板切换 ---------------- */
function setTab(tab) {
  state.tab = tab;
  $$('.tab').forEach((b) => {
    const on = b.dataset.tab === tab;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  });
  $$('.panel').forEach((p) => { p.hidden = p.id !== `panel-${tab}`; });
  ({ detail: renderDetail, map: renderMap, matrix: renderMatrix, draft: renderDraft, data: renderData })[tab]?.();
}

function renderAll() {
  document.documentElement.dataset.theme = state.theme;
  $('#btn-theme').innerHTML = state.theme === 'dark' ? ICON.sun : ICON.moon;
  $('#btn-theme').setAttribute('aria-label', state.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题');
  $('#k-clusters').value = state.k;
  $('#k-out').textContent = state.k;
  $('#sort').value = state.sort;
  $('#q').value = state.q;
  $('#year-from').value = state.yearFrom;
  renderTally();
  renderList();
  setTab(state.tab);
  reanalyze();
}

/* ---------------- 事件 ---------------- */
const patchCard = (it) => {
  const card = $(`.card[data-id="${it.id}"]`);
  if (card) {
    card.querySelector('.card-title').textContent = it.title;
    card.querySelector('.dot').className = `dot s-${it.status}`;
    card.querySelector('.card-meta i').textContent = it.year || '—';
  }
  renderTally();
};

function selectItem(id, { tab = 'detail' } = {}) {
  state.selectedId = id;
  $$('.card').forEach((c) => c.setAttribute('aria-selected', String(c.dataset.id === id)));
  $$('.matrix tbody tr').forEach((r) => r.classList.toggle('sel', r.dataset.row === id));
  if (tab) setTab(tab);
  else if (state.tab === 'detail') renderDetail();
  save();
}

function doExport(filename, text, mime = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`已下载 ${filename}`, 'ok');
}

async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label}已复制`, 'ok');
  } catch {
    toast('浏览器拒绝了剪贴板权限，请手动选中复制', 'bad');
  }
}

function importText(text, { replace = false } = {}) {
  const { items, format } = autoParse(text);
  if (!items.length) { toast('没解析出任何条目，检查一下格式或粘贴内容', 'bad'); return; }
  if (replace) {
    state.items = items;
  } else {
    const merged = mergeInto(state.items, items);
    state.items = merged.items;
    toast(`导入 ${format}：新增 ${merged.added} 篇${merged.updated ? `，补全 ${merged.updated} 篇字段` : ''}`, 'ok');
  }
  state.selectedId ||= state.items[0]?.id;
  save();
  renderAll();
  if (replace) toast(`载入 ${items.length} 篇`, 'ok');
}

function addManual() {
  const title = $('#n-title').value.trim();
  if (!title) { toast('标题不能为空', 'bad'); $('#n-title').focus(); return; }
  const it = blankItem({
    title,
    authors: $('#n-authors').value.split(/[;；]|\s+and\s+/i).map((s) => s.trim()).filter(Boolean),
    year: $('#n-year').value || null,
    venue: $('#n-venue').value.trim(),
  });
  state.items.push(it);
  state.selectedId = it.id;
  save();
  renderAll();
  setTab('detail');
  toast('已添加，接着填精读矩阵', 'ok');
}

/** 合并时保留信息更足的一条：先看精读深度，再看矩阵已填格数、字段完整度 */
function richness(it) {
  const depth = { done: 30, reading: 20, todo: 10 }[it.status] || 0;
  const cells = Object.values(it.matrix || {}).filter(Boolean).length * 3;
  const fields = [it.abstract, it.doi, it.url, it.venue, it.year].filter(Boolean).length;
  return depth + cells + fields + (it.keywords?.length ? 1 : 0);
}

function mergePair(aId, bId) {
  const a = state.items.find((i) => i.id === aId);
  const b = state.items.find((i) => i.id === bId);
  if (!a || !b) return;
  const keep = richness(a) >= richness(b) ? a : b;
  const drop = keep === a ? b : a;
  for (const f of ['abstract', 'doi', 'url', 'venue', 'year']) if (!keep[f] && drop[f]) keep[f] = drop[f];
  keep.keywords = [...new Set([...(keep.keywords || []), ...(drop.keywords || [])])];
  if (keep.authors.length < drop.authors.length) keep.authors = drop.authors;
  for (const [k, v] of Object.entries(drop.matrix)) if (!keep.matrix[k] && v) keep.matrix[k] = v;
  state.items = state.items.filter((i) => i.id !== drop.id);
  if (state.selectedId === drop.id) state.selectedId = keep.id;
  save();
  renderAll();
  toast(`已合并为 [${callOf(keep.id)}]，保留信息更足的一条，其余字段已补齐`, 'ok');
}

/* 顶层事件委托 */
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-status],[data-status-set],[data-open],[data-act],[data-cluster-filter],[data-cluster-pick],[data-import],[data-kw-del],[data-clear],.tab,.card');
  if (!t) return;

  if (t.dataset.statusSet) {
    const cur = state.items.find((i) => i.id === state.selectedId);
    if (cur) {
      cur.status = t.dataset.statusSet;
      save();
      renderDetail();
      renderList();
      reanalyze();
    }
    return;
  }
  if (t.dataset.status) { state.status = t.dataset.status; renderList(); if (state.tab === 'matrix') renderMatrix(); return; }
  if (t.dataset.open) { selectItem(t.dataset.open); return; }
  if (t.dataset.clusterFilter) {
    state.clusterFilter = t.dataset.clusterFilter === 'noise' ? 'noise' : +t.dataset.clusterFilter;
    renderList();
    setTab('detail');
    toast('左栏已切到这一簇的文献', 'ok');
    return;
  }
  if (t.dataset.kwDel) {
    const it = state.items.find((i) => i.id === state.selectedId);
    it.keywords = it.keywords.filter((k) => k !== t.dataset.kwDel);
    save(); renderDetail(); reanalyze();
    return;
  }
  // data-clear 是空值属性，dataset 拿到的是空串，不能用真值判断
  if (t.dataset.clear !== undefined) { state.q = ''; state.status = 'all'; state.yearFrom = ''; state.clusterFilter = null; $('#q').value = ''; $('#year-from').value = ''; renderList(); if (state.tab === 'matrix') renderMatrix(); return; }
  if (t.dataset.import) {
    importKind = t.dataset.import;
    $$('[data-import]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.import === importKind)));
    const box = $('#raw-in');
    box.placeholder = IMPORT_PLACEHOLDER[importKind];
    if (!box.value) box.focus();
    return;
  }
  if (t.classList.contains('tab')) { setTab(t.dataset.tab); return; }
  if (t.classList.contains('card')) { selectItem(t.dataset.id, { tab: state.tab === 'map' ? 'detail' : null }); return; }

  const act = t.dataset.act;
  if (!act) return;
  const cur = state.items.find((i) => i.id === state.selectedId);

  switch (act) {
    case 'demo': importText(JSON.stringify({ items: demoItems() })); break;
    case 'replace-demo':
      if (confirm('用示例库替换当前文献库？当前条目（含你填的精读矩阵）会被清除。')) {
        state.items = demoItems();
        state.selectedId = state.items[0].id;
        save(); renderAll(); toast('已替换为示例库（虚构条目）', 'ok');
      }
      break;
    case 'parse': {
      const v = $('#raw-in').value;
      if (!v.trim()) { toast('先粘贴内容', 'bad'); break; }
      importText(v);
      $('#raw-in').value = '';
      break;
    }
    case 'add': addManual(); break;
    case 'merge': mergePair(t.dataset.a, t.dataset.b); break;
    case 'exp-json': doExport(`综述台-文献库-${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify({ exportedAt: new Date().toISOString(), items: state.items }, null, 2), 'application/json'); break;
    case 'exp-bib': doExport(`综述台-${state.items.length}篇.bib`, state.items.map(formatBibTeX).join('\n\n'), 'application/x-bibtex'); break;
    case 'exp-csv': doExport(`综述台-精读矩阵.csv`, toCSV(state.items), 'text/csv;charset=utf-8'); break;
    case 'wipe':
      if (confirm(`确定清空 ${state.items.length} 篇文献及其精读矩阵？此操作不可撤销。`)) {
        state.items = []; state.selectedId = null; state.clusterFilter = null;
        save(); renderAll(); toast('文献库已清空', 'ok');
      }
      break;
    case 'copy-cite': copy(formatGBT(cur), '著录条目'); break;
    case 'copy-bib': copy(formatBibTeX(cur), 'BibTeX'); break;
    case 'prev': case 'next': {
      const i = callOf(cur.id) - 1 + (act === 'next' ? 1 : -1);
      if (state.items[i]) selectItem(state.items[i].id);
      break;
    }
    case 'del': {
      if (!cur || !confirm(`删除「${cur.title.slice(0, 40)}…」？`)) break;
      const idx = state.items.findIndex((i) => i.id === cur.id);
      state.items.splice(idx, 1);
      state.selectedId = state.items[Math.max(0, idx - 1)]?.id || null;
      save(); renderAll(); toast('已删除，后续编号自动前移');
      break;
    }
    case 'copy-draft': copy($('#draft-text').value, '综述草稿'); break;
    case 'download-draft': doExport(`${state.draft.title || '综述草稿'}.md`, $('#draft-text').value, 'text/markdown;charset=utf-8'); break;
    case 'regen': generateDraft(); toast('已按当前选项重生成'); break;
    case 'help': openHelp(); break;
    case 'close-help': $('#modal').hidden = true; break;
  }
});

const patchCitation = () => {
  const cur = state.items.find((i) => i.id === state.selectedId);
  if (!cur) return;
  if ($('#cite-gbt')) $('#cite-gbt').textContent = formatGBT(cur);
  if ($('#cite-apa')) $('#cite-apa').textContent = formatAPA(cur);
};

const commitField = debounce(() => { patchCitation(); save(); reanalyze(); }, 300);

document.addEventListener('input', (e) => {
  const el = e.target;
  const cur = state.items.find((i) => i.id === state.selectedId);

  if (el.id === 'q') { state.q = el.value; debouncedList(); return; }
  if (el.id === 'year-from') { state.yearFrom = el.value; debouncedList(); return; }
  if (el.dataset.matrix && cur) {
    const id = el.dataset.id ? state.items.find((i) => i.id === el.dataset.id) : cur;
    id.matrix[el.dataset.matrix] = el.value;
    el.classList.toggle('empty', !el.value);
    if (id === cur) {
      const label = $(`#m-${el.dataset.matrix}`)?.parentElement?.querySelector('em');
      if (label) label.textContent = el.value ? '已填' : '待填';
    }
    commitField();
    return;
  }
  if (el.dataset.field && cur) {
    const f = el.dataset.field;
    let v = el.value;
    if (f === 'authors') cur.authors = v.split(/[;；]|\s+and\s+/i).map((s) => s.trim()).filter(Boolean);
    else if (f === 'year') cur.year = +v || null;
    else cur[f] = v;
    patchCard(cur);
    if (f === 'abstract') {
      const label = el.closest('.field-block')?.previousElementSibling?.querySelector('span');
      if (label) label.textContent = `${el.value.length} 字`;
    }
    commitField();
    return;
  }
  if (el.dataset.draft) {
    // 只更新状态与输出，不重建控件：重建会让正在输入的标题丢焦点
    const k = el.dataset.draft;
    state.draft[k] = el.type === 'checkbox' ? el.checked : el.value;
    state.draftDirty = false;
    generateDraft();
    save();
    return;
  }
  if (el.id === 'k-clusters') { state.k = +el.value; $('#k-out').textContent = el.value; reanalyzeFast(); return; }
  if (el.id === 'draft-text') { state.draftDirty = true; return; }
});

const debouncedList = debounce(() => { renderList(); if (state.tab === 'matrix') renderMatrix(); renderFilterNote(); }, 220);

function setField(field, value) {
  const cur = state.items.find((i) => i.id === state.selectedId);
  if (!cur) return;
  cur[field] = value;
  save();
  renderDetail();
  reanalyze();
}

document.addEventListener('change', (e) => {
  const el = e.target;
  if (el.id === 'sort') { state.sort = el.value; renderList(); if (state.tab === 'matrix') renderMatrix(); save(); return; }
  if (el.id === 'file-in' && el.files?.length) {
    [...el.files].forEach((f) => {
      const r = new FileReader();
      r.onload = () => importText(String(r.result));
      r.readAsText(f);
    });
    el.value = '';
    return;
  }
  if (el.dataset.clusterPick) {
    const i = +el.dataset.clusterPick;
    const all = state.analysis.clusters.map((c) => c.index);
    const picked = new Set(state.draft.clusterIds || all);
    el.checked ? picked.add(i) : picked.delete(i);
    state.draft.clusterIds = [...picked].sort((a, b) => a - b);
    if (!picked.size) toast('没选任何主题簇，草稿只会剩下引言和参考文献');
    state.draftDirty = false;
    renderDraftControls();
    generateDraft();
    save();
  }
  if (el.id === 'f-type') setField('type', el.value);
});

document.addEventListener('keydown', (e) => {
  const tag = document.activeElement?.tagName;
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(tag);
  if (e.key === 'Escape') {
    if (!$('#modal').hidden) { $('#modal').hidden = true; return; }
    if (document.body.classList.contains('rail-open')) { document.body.classList.remove('rail-open'); return; }
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#q').focus(); $('#q').select(); return; }
  if (typing) {
    if (e.key === 'Enter' && document.activeElement.id === 'f-kw') {
      const cur = state.items.find((i) => i.id === state.selectedId);
      const v = e.target.value.trim();
      if (v && cur) {
        cur.keywords = [...new Set([...cur.keywords, ...v.split(/[;；,，]/).map((s) => s.trim()).filter(Boolean)])];
        e.target.value = '';
        save(); renderDetail(); $('#f-kw')?.focus(); reanalyze();
      }
      e.preventDefault();
    }
    return;
  }
  if (/^[1-5]$/.test(e.key)) { setTab(['detail', 'map', 'matrix', 'draft', 'data'][+e.key - 1]); return; }
  if (e.key === '/' ) { e.preventDefault(); $('#q').focus(); return; }
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && state.items.length) {
    const list = filtered();
    const i = list.findIndex((x) => x.id === state.selectedId);
    const ni = Math.min(Math.max(i + (e.key === 'ArrowDown' ? 1 : -1), 0), list.length - 1);
    if (list[ni]) { selectItem(list[ni].id, { tab: state.tab === 'detail' ? 'detail' : null }); list[ni] && $(`.card[data-id="${list[ni].id}"]`)?.scrollIntoView({ block: 'nearest' }); }
    e.preventDefault();
  }
});

/* 拖拽导入 */
let dragDepth = 0;
document.addEventListener('dragenter', (e) => { if (e.dataTransfer?.types?.includes('Files')) { dragDepth++; $('#dropzone').hidden = false; } });
document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; $('#dropzone').hidden = true; } });
document.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  $('#dropzone').hidden = true;
  [...(e.dataTransfer?.files || [])].forEach((f) => {
    const r = new FileReader();
    r.onload = () => importText(String(r.result));
    r.readAsText(f);
  });
});

/* 说明弹窗 */
function helpHTML() {
  return `
    <ol>
      <li><strong>先看到效果：</strong>点顶栏「载入示例库」，几十篇虚构文献会立刻进入主题图谱、对比矩阵和综述草稿。示例条目全部是虚构的，只是为了让你 30 秒看完功能。</li>
      <li><strong>换成你自己的：</strong>到「数据与体检」，把 Zotero / CNKI 导出的 <em>.bib</em> 或 <em>.ris</em> 整段粘进去（也可以直接拖文件进来）。同 DOI、同标题会自动合并。</li>
      <li><strong>标记你读到哪了：</strong>左栏搜索、按状态筛选；选中一篇后在「单篇精读」里把状态改成在读或已读。</li>
      <li><strong>填精读矩阵：</strong>方法 / 数据 / 结论 / 局限四格，在「对比矩阵」里可以整表连续填，失焦即存。</li>
      <li><strong>出综述骨架：</strong>「综述草稿」里挑主题簇、选著录格式，右侧生成可直接编辑的 Markdown，一键复制或下载。<em>只有已读且填了矩阵的文献才会写成具体论断</em>，其余留成待补条目。</li>
    </ol>
    <h3>顺手记住</h3>
    <ul>
      <li><kbd>⌘/Ctrl</kbd> + <kbd>K</kbd> 或 <kbd>/</kbd> 回到搜索框；<kbd>↑</kbd><kbd>↓</kbd> 在文献间跳；<kbd>1</kbd>–<kbd>5</kbd> 切工作区。</li>
      <li>簇数滑块在「主题图谱」右上角：调大拆得细，调小合并成几大方向。</li>
      <li>数据只存在这台设备的浏览器里，换电脑用「数据与体检 → JSON 备份」带走。</li>
    </ul>`;
}

function openHelp() {
  $('#modal-body').innerHTML = helpHTML();
  $('#modal').hidden = false;
  $('#btn-modal-close').focus();
}

/* ---------------- 启动 ---------------- */
function boot() {
  $('#icon-search').innerHTML = ICON.search;
  const had = load();
  if (!had && !state.items.length) {
    state.items = demoItems();
    state.selectedId = state.items[0].id;
    toast(`首次进入已载入演示文献库（${state.items.length} 篇虚构条目），可随时换成你自己的 .bib`, '', { label: '导入我的文献', run: () => { setTab('data'); setTimeout(() => $('#raw-in')?.focus(), 80); } });
  }
  renderAll();

  $('#btn-demo').onclick = () => {
    if (state.items.some((i) => !i.demo) && !confirm('当前库里已有你自己的文献。示例条目会追加在后面（同标题自动合并），继续？')) return;
    importText(JSON.stringify({ items: demoItems() }));
  };
  $('#btn-help').dataset.act = 'help';
  $('#btn-modal-close').onclick = () => { $('#modal').hidden = true; };
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('#modal').hidden = true; });
  $('#btn-theme').onclick = () => { state.theme = state.theme === 'dark' ? 'light' : 'dark'; renderAll(); save(); };
  $('#btn-rail-close').onclick = () => document.body.classList.remove('rail-open');
  $('#btn-recluster').onclick = () => { reanalyzeFast(); toast('已按当前 k 重新聚类'); };
  $('#btn-copy-draft').dataset.act = 'copy-draft';
  $('#btn-download-draft').dataset.act = 'download-draft';
  $('#btn-export-csv').dataset.act = 'exp-csv';

  // 移动端：卡片列表默认收起，用顶栏搜索旁按钮唤出
  $('#list').addEventListener('dblclick', () => document.body.classList.toggle('rail-open'));
  if (!had) save();
}

const reanalyzeFast = () => {
  state.analysis = state.items.length > 1 ? cluster(state.items, state.k) : { clusters: [], noise: state.items.map((i) => i.id), topTerms: [] };
  renderMap();
  renderTally();
  renderList();
  syncDetailBadge();
  if (state.tab === 'draft') renderDraft();
};

// 顶栏在小屏时提供文献列表入口
const railToggle = document.createElement('button');
railToggle.className = 'btn btn-ghost btn-sm';
railToggle.type = 'button';
railToggle.textContent = '文献列表';
railToggle.onclick = () => document.body.classList.toggle('rail-open');
$('#btn-demo').before(railToggle);
if (window.innerWidth > 900) railToggle.hidden = true;
window.addEventListener('resize', debounce(() => { railToggle.hidden = window.innerWidth > 900; }, 200));

boot();
