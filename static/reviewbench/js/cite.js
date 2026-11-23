/* 著录格式与综述骨架生成
   GB/T 7714（简化实现）/ APA 7 基本格式 / BibTeX 回写
   综述草稿只使用用户自己填进精读矩阵的内容，缺的就标「待补」，不编造论断。 */

const isCJK = (s) => /[㐀-鿿]/.test(s);

/** 把 "Jane Doe" / "Doe, J." / "张三" 统一拆成 {last, given} */
export function splitName(full) {
  const s = String(full || '').replace(/\s+/g, ' ').trim();
  if (!s) return { last: '', given: '' };
  if (s.includes(',')) {
    const [last, ...rest] = s.split(',');
    return { last: last.trim(), given: rest.join(' ').trim() };
  }
  if (isCJK(s)) return { last: s, given: '' };
  const parts = s.split(' ');
  return { last: parts.pop(), given: parts.join(' ') };
}

const initials = (given) => given.split(/[\s.\-]+/).filter(Boolean).map((p) => `${p[0].toUpperCase()}.`).join(' ');

const gbtName = (full) => {
  const { last, given } = splitName(full);
  if (isCJK(last)) return given ? `${last}${given}` : last;
  return `${(last || '').toUpperCase()}${given ? ' ' + initials(given) : ''}`;
};

const apaName = (full) => {
  const { last, given } = splitName(full);
  return given ? `${last}, ${initials(given)}` : last;
};

const docKind = (it) => ({ journal: 'J', conference: 'C', book: 'M', thesis: 'D', preprint: 'R', web: 'EB/OL', other: 'A' }[it.type] || 'A');

export function formatGBT(it) {
  const authors = (it.authors || []).map(gbtName).filter(Boolean);
  const who = authors.length ? (authors.length > 3 ? `${authors.slice(0, 3).join(', ')}，等` : authors.join(', ')) : '佚名';
  const kind = docKind(it);
  const container = it.venue ? (kind === 'J' ? `${it.venue}` : it.venue) : kind === 'EB/OL' ? [it.url].filter(Boolean) : '';
  const pieces = [`${who}. ${it.title}[${kind}]`];
  if (container) pieces.push(String(container));
  if (it.year) pieces.push(String(it.year));
  let out = pieces.filter(Boolean).join('. ');
  if (kind === 'EB/OL' && it.url) out += `. ${it.url}`;
  else if (it.doi) out += `. https://doi.org/${it.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//, '')}`;
  return out.replace(/\.\./g, '.').replace(/(\D)\.$/, '$1');
}

export function formatAPA(it) {
  const names = (it.authors || []).map(apaName).filter(Boolean);
  let who;
  if (!names.length) who = '';
  else if (names.length === 1) who = names[0];
  else if (names.length <= 20) who = `${names.slice(0, -1).join(', ')}, & ${names.at(-1)}`;
  else who = `${names.slice(0, 19).join(', ')}, ... ${names.at(-1)}`;
  const year = it.year ? `(${it.year})` : '(n.d.)';
  const container = it.venue ? `. *${it.venue}*` : '';
  const link = it.doi ? `. https://doi.org/${it.doi.replace(/^https?:\/\/(dx\.)?doi\.org\//, '')}`
    : it.url ? `. ${it.url}` : '';
  return `${who} ${year}. ${it.title}${container}${link}`.replace(/\s+/g, ' ').trim();
}

export function formatBibTeX(it) {
  const key = `${(splitName(it.authors[0] || 'anon').last || 'anon').replace(/[^\p{L}]/gu, '').toLowerCase()}${it.year || ''}`;
  const typeMap = { journal: 'article', conference: 'inproceedings', book: 'book', thesis: 'phdthesis', preprint: 'misc', web: 'misc', other: 'misc' };
  const fields = [
    ['author', (it.authors || []).join(' and ')],
    ['title', `{${it.title}}`],
    ['year', it.year],
    [it.type === 'conference' ? 'booktitle' : 'journal', it.venue],
    ['doi', it.doi],
    ['url', it.url],
    ['abstract', it.abstract],
    ['keywords', (it.keywords || []).join(', ')],
  ].filter(([, v]) => v !== undefined && v !== '' && v !== null);
  return `@${typeMap[it.type] || 'misc'}{${key},\n${fields.map(([k, v]) => `  ${k} = {${String(v).replace(/[{}]/g, '')}}`).join(',\n')}\n}`;
}

export const FORMATTERS = {
  gbt: { label: 'GB/T 7714（简化）', fn: formatGBT },
  apa: { label: 'APA 7', fn: formatAPA },
};

/* ---------------- 综述草稿 ---------------- */

const SENT = {
  lead: (label, n, span) => `本节围绕${label}展开，共纳入 ${n} 篇工作${span ? `（${span}）` : ''}。`,
  /** 只把真正填过的字段写进句子；缺的字段整句省略，不留「未记录」这类假话 */
  claim: (who, year, m) => {
    const parts = [];
    if (m.method) parts.push(`从「${m.method}」切入`);
    if (m.data) parts.push(`基于${m.data}`);
    if (m.findings) parts.push(`报告${m.findings}`);
    const stem = `${who}（${year || '—'}）`;
    if (!parts.length) return `${stem}已纳入本节，但方法、数据与结论尚未填入精读矩阵，暂不下结论`;
    let s = `${stem}${parts.join('，')}`;
    if (m.limits) s += `；其局限在于${m.limits}`;
    return s;
  },
};

function authorShort(it) {
  const a = it.authors?.[0];
  if (!a) return '某课题组';
  const { last } = splitName(a);
  return isCJK(last) ? last : last.toUpperCase();
}

/** 顺序编码制：[编号] 按文献在正文中首次出现的顺序分配，与文末参考文献表严格一致 */
export function buildDraft({ items, clusters, noise = [], topTerms = [], opts = {}, title }) {
  const fmt = FORMATTERS[opts.format || 'gbt'].fn;
  const byId = new Map(items.map((i) => [i.id, i]));
  const numbers = new Map();
  const cite = (id) => {
    if (!numbers.has(id)) numbers.set(id, numbers.size + 1);
    return `[${numbers.get(id)}]`;
  };

  const wanted = clusters.filter((c) => opts.clusterIds ? opts.clusterIds.includes(c.index) : true);
  const onlyDone = opts.onlyDone !== false;
  const now = new Date();
  const allYears = items.map((i) => i.year).filter(Boolean);
  const span = allYears.length ? `${Math.min(...allYears)}–${Math.max(...allYears)}` : '';
  const doneCount = items.filter((i) => i.status === 'done').length;

  const lines = [];
  lines.push(`# ${title || '文献综述草稿'}`);
  lines.push('');
  lines.push(`> 由综述台生成于 ${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} ｜ 纳入 ${items.length} 篇 ｜ 主题簇 ${wanted.length} 个 ｜ 精读完成 ${doneCount}/${items.length}`);
  lines.push('>');
  lines.push('> 这是**写作骨架**，不是成稿：每一句具体论断都取自你在「对比矩阵」里填写的方法、数据、结论与局限。没填的地方会明确标成待补，工具不会替你编造结果。');
  lines.push('');

  let bodyStart = 1;
  const hotTerms = (topTerms || []).slice(0, 3).map((t) => t.term).filter(Boolean);
  if (opts.intro !== false) {
    lines.push('## 1 引言');
    lines.push('');
    lines.push(`本综述梳理了 ${items.length} 篇${span ? `${span} 年间` : ''}的相关工作，围绕${hotTerms.length ? `${hotTerms.join('、')}等主题` : '若干主题'}组织材料。下文按主题分节讨论各条技术路线的做法、证据强度与尚存缺口，最后归纳可继续切入的问题。`);
    lines.push('');
    lines.push('<!-- TODO：在这里补上研究问题（RQ）、检索范围（数据库 / 时间窗 / 纳入排除标准）。这部分工具无法代填。 -->');
    lines.push('');
    bodyStart = 2;
  }

  let idx = bodyStart;
  let made = 0, pending = 0;
  const gaps = [];

  for (const c of wanted) {
    lines.push(`## ${idx} ${c.label}`);
    lines.push('');
    lines.push(SENT.lead(c.label, c.members.length, c.yearFrom ? `${c.yearFrom}–${c.yearTo}` : ''));
    lines.push('');

    const members = c.members
      .map((id) => byId.get(id))
      .filter(Boolean)
      .sort((a, b) => (a.year || 0) - (b.year || 0));

    for (const it of members) {
      const m = it.matrix || {};
      const hasMeat = onlyDone ? (m.method || m.findings) && it.status !== 'todo' : (m.method || m.findings || m.data || m.limits);
      if (!hasMeat) {
        pending++;
        lines.push(`- ${authorShort(it)}（${it.year || '—'}）*${it.title}* ${cite(it.id)} —— 待精读，补充方法与结论后改写进本节。`);
        continue;
      }
      made++;
      lines.push(`- ${SENT.claim(authorShort(it), it.year, m)} ${cite(it.id)}（${it.title}）`);
    }

    lines.push('');
    if (opts.bridge !== false) {
      lines.push(`<!-- 承接句建议：本节 ${c.filled}/${c.members.length} 篇已填矩阵；说明这一路线与上一节的差别（问题设定 / 数据来源 / 评价指标），再引出下一节。 -->`);
      lines.push('');
    }
    if (c.done === 0) gaps.push(`「${c.label}」共 ${c.members.length} 篇，尚无一篇标记为已读。`);
    else if (c.filled < c.members.length) gaps.push(`「${c.label}」还有 ${c.members.length - c.filled} 篇没填精读矩阵，这节只能写成罗列。`);
    idx++;
  }

  if (noise.length && opts.noise !== false) {
    lines.push(`## ${idx} 尚未归入主题的文献`);
    lines.push('');
    lines.push(`以下 ${noise.length} 篇与上面各主题的向量相似度都偏低，可能是综述的边缘材料，也可能提示还缺一整个主题：`);
    lines.push('');
    for (const id of noise) {
      const it = byId.get(id);
      if (!it) continue;
      lines.push(`- ${authorShort(it)}（${it.year || '—'}）${it.title} ${cite(it.id)}`);
    }
    lines.push('');
    idx++;
  }

  if (opts.gaps !== false) {
    const recent = items.filter((i) => i.year && i.year >= now.getFullYear() - 1).length;
    lines.push(`## ${idx} 研究空白与可切入的问题`);
    lines.push('');
    const bullets = [];
    if (gaps.length) bullets.push(...gaps.map((g) => `- **材料缺口** ${g}`));
    if (!recent) bullets.push(`- **时间缺口** 最近两年（${now.getFullYear() - 1}–${now.getFullYear()}）只有 ${recent} 篇，需补检索最新工作，否则结论容易过时。`);
    const thin = clusters.filter((c) => c.members.length <= 2);
    if (thin.length) bullets.push(`- **主题缺口** ${thin.map((c) => `「${c.label}」仅 ${c.members.length} 篇`).join('；')}，样本过薄，暂时不宜下判断。`);
    const noId = items.filter((i) => !i.doi && !i.url).length;
    if (noId) bullets.push(`- **著录缺口** ${noId} 篇缺 DOI 或链接，参考文献表需要人工补齐后才能投稿。`);
    bullets.push('- **待你回答** 上面各路线在评价指标上是否可比？若不可比，综述里要显式说明，不能直接横向排名。');
    lines.push(...bullets);
    lines.push('');
    idx++;
  }

  const cited = [...numbers.entries()];
  if (cited.length && opts.refs !== false) {
    lines.push('## 参考文献');
    lines.push('');
    lines.push('<!-- 采用' + FORMATTERS[opts.format || 'gbt'].label + '，仅简化实现；正式投稿请再用 Zotero / 知网研学核对一次 -->');
    lines.push('');
    const ordered = cited.sort((a, b) => a[1] - b[1]);
    for (const [id, n] of ordered) {
      const it = byId.get(id);
      if (it) lines.push(`[${n}] ${fmt(it)}`);
    }
    lines.push('');
  }

  return {
    markdown: lines.join('\n'),
    cited: numbers.size,
    made,
    pending,
    sections: idx - 1,
  };
}

export function toCSV(items) {
  const head = ['call', 'status', 'year', 'title', 'authors', 'venue', 'doi', 'method', 'data', 'findings', 'limits', 'notes', 'keywords'];
  const esc = (v) => {
    const s = String(v ?? '');
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const rows = items.map((it, i) => [
    i + 1,
    { todo: '未读', reading: '在读', done: '已读' }[it.status],
    it.year || '',
    it.title,
    (it.authors || []).join('; '),
    it.venue || '',
    it.doi || '',
    it.matrix?.method || '',
    it.matrix?.data || '',
    it.matrix?.findings || '',
    it.matrix?.limits || '',
    (it.notes || '').replace(/\n/g, ' '),
    (it.keywords || []).join('; '),
  ].map(esc).join(','));
  return `${head.join(',')}\n${rows.join('\n')}`;
}
