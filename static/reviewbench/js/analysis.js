/* 前端文本分析：中英混合分词 → TF-IDF 加权 → k-medoids 主题聚类
   另含相似标题查重、年份分布与文献库体检。全部确定性计算，可复现。 */

const STOP_EN = new Set(`a an and are as at be been but by can could did do does for from had has have he her his how i if in into is it its may not of on or our so than that the their them then there these they this to was we were what when which who will with would you your too also very much many more most such via per among within using used use one two three four five six seven eight nine ten hundred thousand percent might must shall should etc fig figure table based study studies paper results result new compared comparison versus likely despite`.split(/\s+/));

/* 单字停用：避免 CJK bigram 里大量「的了是在和与及对为」等虚词 */
const STOP_CN = new Set('的了是在和与及对为有着或等之中并也都就不呢吗吧把被通过进行以及及其其中该能够可能因为所以但是虽然然而因此并且而且此外有关关于本文作者我们他们它这那一种一些方式问题情况内容数据结果方法'.split(''));

/* 二字组层面的虚词：这些词在学术中文里几乎每篇都有，当主题名只会稀释区分度 */
const STOP_CN_BI = new Set('如何 是否 已经 可以 需要 认为 主要 其中 上述 以下 首先 其次 最后 通过 由于 对于 关于 一种 一些 以及 及其 因此 然而 但是 虽然 因为 所以 下降 提高 增加 减少 显著 平均 显示 表明 分别 进一步 本文 作者 我们 他们 研究 分析'.split(' '));

const WORD_RE = /[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu;
const CJK_RE = /[㐀-䶿一-鿿぀-ヿ]/;

/** 英文取整词，中文取二字组；返回带重复次数的词元序列 */
export function tokenize(text) {
  if (!text) return [];
  const out = [];
  const lower = String(text).toLowerCase();
  for (const raw of lower.match(WORD_RE) || []) {
    const w = raw.replace(/^['’-]+|['’-]+$/g, '');
    if (w.length < 2 || STOP_EN.has(w) || /^\d+$/.test(w)) continue;
    if (CJK_RE.test(w)) {
      const chars = [...w];
      // 中文没有现成切词，二字组是最稳的替代；实测再加三字组会让稀有词元稀释相似度、簇覆盖率下降
      for (let i = 0; i < chars.length - 1; i++) {
        const bi = chars[i] + chars[i + 1];
        if (!STOP_CN.has(chars[i]) && !STOP_CN.has(chars[i + 1]) && !STOP_CN_BI.has(bi)) out.push(bi);
      }
      if (chars.length === 1 && !STOP_CN.has(chars[0])) out.push(chars[0]);
    } else {
      out.push(w);
    }
  }
  return out;
}

/** 标题、关键词权重高于摘要，避免长摘要淹没主题信号 */
const FIELD_WEIGHT = [[(i) => i.title, 3.2], [(i) => (i.keywords || []).join(' '), 2.6], [(i) => i.abstract, 1]];

function docTokens(item) {
  const seq = [];
  for (const [get, w] of FIELD_WEIGHT) {
    const toks = tokenize(get(item) || '');
    const repeat = w >= 3 ? 3 : w >= 2.5 ? 2 : 1;
    for (let r = 0; r < repeat; r++) seq.push(...toks);
  }
  return seq;
}

const dot = (a, b) => {
  const [small, big] = a.size < b.size ? [a, b] : [b, a];
  let s = 0;
  for (const [t, v] of small) {
    const o = big.get(t);
    if (o) s += v * o;
  }
  return s;
};

const norm = (v) => Math.sqrt(dot(v, v)) || 1;

export function cosine(a, b) {
  const d = dot(a, b);
  return d ? d / (norm(a) * norm(b)) : 0;
}

/** 语料向量化：加权词频 × 逆文档频率，再做 L2 归一化 */
export function buildVectors(items) {
  const tf = new Map();     // docId -> Map(term -> count)
  const df = new Map();     // term -> 出现该词的文档数
  for (const it of items) {
    const counts = new Map();
    for (const t of docTokens(it)) counts.set(t, (counts.get(t) || 0) + 1);
    tf.set(it.id, counts);
    for (const t of counts.keys()) df.set(t, (df.get(t) || 0) + 1);
  }
  const N = items.length || 1;
  const idf = new Map();
  for (const [t, c] of df) idf.set(t, Math.log(1 + N / (1 + c)));

  const vectors = new Map();
  for (const it of items) {
    const counts = tf.get(it.id);
    const v = new Map();
    let len = 0;
    // 次线性 TF：长摘要不会因为词多而压过标题信号
    for (const [t, c] of counts) {
      const w = (1 + Math.log(c)) * idf.get(t);
      if (w > 0) { v.set(t, w); len += w * w; }
    }
    const inv = len ? 1 / Math.sqrt(len) : 1;
    for (const [t, w] of v) v.set(t, w * inv);
    vectors.set(it.id, v);
  }

  // 词频表按 idf 排序，供「主题词榜」展示
  const topTerms = [...df.entries()]
    .filter(([, c]) => c >= 2)
    .map(([t, c]) => ({ term: t, df: c, idf: idf.get(t), weight: idf.get(t) * c }))
    .sort((a, b) => b.df - a.df || b.idf - a.idf)
    .slice(0, 24);

  return { vectors, tf, df, idf, topTerms };
}

const humanize = (term) => term.replace(/-/g, ' ');

/**
 * k-medoids 聚类：远点取样定初始中心 → 按余弦相似度分配 → 用组内最中心的成员更新中心，循环到稳定。
 * 与所有中心相似度都过低的条目进入「未归类」，不硬塞进某个主题。
 */
export function cluster(items, k = 5, minSim = 0.02) {
  const { vectors, idf, topTerms } = buildVectors(items);
  const ids = items.map((i) => i.id).filter((id) => vectors.get(id)?.size);
  const empty = { clusters: [], noise: items.map((i) => i.id), topTerms, threshold: minSim, vectors, idf };
  if (ids.length < 2) return empty;

  const byId = new Map(items.map((i) => [i.id, i]));
  const cappedK = Math.max(2, Math.min(k, ids.length - 1));

  // 初始 medoid：先取向量模长最大的（信息量最足），之后每次挑离已选中心最近的相似度最低者
  const medoids = [[...ids].sort((a, b) => norm(vectors.get(b)) - norm(vectors.get(a)))[0]];
  while (medoids.length < cappedK) {
    let pick = null, worst = -1;
    for (const id of ids) {
      if (medoids.includes(id)) continue;
      const nearest = Math.max(...medoids.map((m) => cosine(vectors.get(id), vectors.get(m))));
      if (1 - nearest > worst) { worst = 1 - nearest; pick = id; }
    }
    if (pick == null || worst <= 0) break;
    medoids.push(pick);
  }

  const assign = () => {
    const groups = medoids.map(() => []);
    const noise = [];
    for (const id of ids) {
      let bi = -1, bs = minSim;
      for (let i = 0; i < medoids.length; i++) {
        const s = cosine(vectors.get(id), vectors.get(medoids[i]));
        if (s > bs) { bs = s; bi = i; }
      }
      (bi < 0 ? noise : groups[bi]).push(id);
    }
    return { groups, noise };
  };

  let { groups, noise } = assign();
  for (let iter = 0; iter < 6; iter++) {
    let changed = false;
    for (let i = 0; i < groups.length; i++) {
      const pool = groups[i];
      if (!pool.length) continue;
      // 组内与其他成员相似度总和最大的那个成员成为新 medoid（原 medoid 也在候选里）
      const cand = pool.includes(medoids[i]) ? pool : [...pool, medoids[i]];
      let best = medoids[i], bestSum = -1;
      for (const c of cand) {
        let sum = 0;
        for (const id of pool) if (id !== c) sum += cosine(vectors.get(c), vectors.get(id));
        if (sum > bestSum) { bestSum = sum; best = c; }
      }
      if (best !== medoids[i]) { medoids[i] = best; changed = true; }
    }
    const next = assign();
    groups = next.groups;
    noise = next.noise;
    if (!changed) break;
  }

  // 只有一篇的「主题」不成立，退回未归类
  const singles = groups.filter((g) => g.length < 2).flat();
  noise = [...noise, ...singles];
  const kept = groups.map((g) => (g.length < 2 ? [] : g));

  const clusters = medoids.map((medoid, i) => {
    const members = kept[i] || [];
    const sets = members.map((id) => new Set(vectors.get(id)?.keys() || []));
    const dfLocal = new Map();
    for (const s of sets) for (const t of s) dfLocal.set(t, (dfLocal.get(t) || 0) + 1);
    const terms = [...dfLocal.entries()]
      .filter(([, c]) => t_ok(c, members.length))
      .map(([t, c]) => ({ term: humanize(t), docs: c, weight: c * (idf.get(t) || 0) }))
      .sort((a, b) => b.weight - a.weight)
      .slice(0, 8);
    const years = members.map((id) => byId.get(id).year).filter(Boolean);
    return {
      medoid,
      label: nameCluster(sets, dfLocal, idf, members.length,
        members.map((id) => tokenize(`${byId.get(id).title} ${(byId.get(id).keywords || []).join(' ')}`))),
      terms,
      members,
      yearFrom: years.length ? Math.min(...years) : null,
      yearTo: years.length ? Math.max(...years) : null,
      done: members.filter((id) => byId.get(id).status === 'done').length,
      filled: members.filter((id) => {
        const m = byId.get(id).matrix;
        return m.method || m.data || m.findings || m.limits;
      }).length,
    };
  }).filter((c) => c.members.length)
    .sort((a, b) => b.members.length - a.members.length)
    .map((c, i) => ({ ...c, index: i + 1 }));

  return { clusters, noise, topTerms, threshold: minSim, vectors, idf };
}

/** 词在该簇里是否值得当主题词：小簇允许只出现一次，大簇要求至少两篇共享 */
const t_ok = (c, size) => c >= (size >= 4 ? 2 : 1);

/**
 * 簇名：取区分度最高的词作中心词（用 log(簇内篇数) × idf，避免「学生/研究」这类高频词压过术语）；
 * 若第二个词有一半以上成员与中心词同篇出现，就当成术语短语连写，并按原文语序排列；
 * 否则用「·」并列两个词。
 */
function nameCluster(sets, dfLocal, idf, size, texts) {
  const ranked = [...dfLocal.entries()]
    .filter(([t, c]) => t.length >= 2 && t_ok(c, size))
    .map(([t, c]) => ({ t, c, score: Math.log(1 + c) * (idf.get(t) || 0) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, 6);
  if (!ranked.length) return '未命名主题';
  const head = ranked[0].t;
  let partner = '';
  for (const cand of ranked.slice(1)) {
    const both = sets.filter((s) => s.has(head) && s.has(cand.t)).length;
    if (both / dfLocal.get(head) >= 0.5) { partner = cand.t; break; }
  }
  if (!partner) {
    const second = ranked[1] ? humanize(ranked[1].t) : '';
    return second ? `${humanize(head)} · ${second}` : humanize(head);
  }
  // 按术语在原文里的先后排成短语
  let ha = 0, hb = 0, n = 0;
  for (const toks of texts) {
    const ia = toks.indexOf(head), ib = toks.indexOf(partner);
    if (ia < 0 || ib < 0) continue;
    ha += ia; hb += ib; n++;
  }
  const [a, b] = n && hb < ha ? [partner, head] : [head, partner];
  return `${humanize(a)} ${humanize(b)}`;
}

/** 标题词集 Jaccard + 同年同第一作者加成，找出可能重复录入的条目 */
export function findDuplicates(items) {
  const sets = items.map((it) => ({
    it,
    set: new Set(tokenize(it.title).filter((t) => t.length > 2)),
  }));
  const pairs = [];
  for (let i = 0; i < sets.length; i++) {
    for (let j = i + 1; j < sets.length; j++) {
      const a = sets[i], b = sets[j];
      if (a.it.title === b.it.title) { pairs.push({ a: a.it, b: b.it, sim: 1 }); continue; }
      if (!a.set.size || !b.set.size) continue;
      let inter = 0;
      for (const t of a.set) if (b.set.has(t)) inter++;
      let sim = inter / (a.set.size + b.set.size - inter);
      if (a.it.year && a.it.year === b.it.year) sim += 0.12;
      const fa = (a.it.authors[0] || '').toLowerCase();
      const fb = (b.it.authors[0] || '').toLowerCase();
      if (fa && fa === fb) sim += 0.1;
      if (a.it.doi && a.it.doi === b.it.doi) sim = 1;
      if (sim >= 0.42) pairs.push({ a: a.it, b: b.it, sim: Math.min(sim, 1) });
    }
  }
  return pairs.sort((x, y) => y.sim - x.sim).slice(0, 12);
}

export function yearHistogram(items) {
  const years = items.map((i) => i.year).filter(Boolean);
  if (!years.length) return { from: 0, to: 0, buckets: [], span: 0 };
  const from = Math.min(...years), to = Math.max(...years);
  const span = Math.max(to - from, 1);
  const buckets = Array.from({ length: span + 1 }, (_, i) => ({ year: from + i, n: 0 }));
  for (const y of years) buckets[y - from].n++;
  return { from, to, buckets, span };
}

/** 文献库体检：读得够不够新、覆盖是否偏科、字段是否完整 */
export function healthCheck(items) {
  const n = items.length;
  const nowYear = new Date().getFullYear();
  const years = items.map((i) => i.year).filter(Boolean);
  const recent = years.filter((y) => y >= nowYear - 3).length;
  const recentShare = years.length ? recent / years.length : 0;
  const done = items.filter((i) => i.status === 'done').length;
  const noAbstract = items.filter((i) => !i.abstract).length;
  const noId = items.filter((i) => !i.doi && !i.url).length;
  const noMatrix = items.filter((i) => {
    const m = i.matrix;
    return !(m.method && m.findings);
  }).length;
  const authors = new Map();
  items.forEach((i) => (i.authors[0] || '').toLowerCase() && authors.set((i.authors[0] || '').toLowerCase(), (authors.get((i.authors[0] || '').toLowerCase()) || 0) + 1));
  const topAuthor = [...authors.entries()].sort((a, b) => b[1] - a[1])[0];
  const concentration = n && topAuthor ? topAuthor[1] / n : 0;

  return [
    {
      key: 'volume',
      label: '文献总量',
      value: n,
      unit: '篇',
      level: n >= 25 ? 'ok' : n >= 10 ? 'warn' : 'bad',
      detail: n >= 25 ? '样本量足够支撑一篇小型综述的文献综述章节。' : '一篇课程论文级别的综述通常需要 20–40 篇，目前还偏少。',
    },
    {
      key: 'recency',
      label: '近三年占比',
      value: Math.round(recentShare * 100),
      unit: '%',
      level: recentShare >= 0.35 ? 'ok' : recentShare >= 0.15 ? 'warn' : 'bad',
      detail: `最新三年（${nowYear - 3}–${nowYear}）有 ${recent} 篇。占比低于 15% 时，综述容易被评审查新不查旧。`,
    },
    {
      key: 'reading',
      label: '精读完成度',
      value: n ? Math.round((done / n) * 100) : 0,
      unit: '%',
      level: done / (n || 1) >= 0.4 ? 'ok' : done / (n || 1) >= 0.15 ? 'warn' : 'bad',
      detail: `标记为「已读」的有 ${done} 篇。综述草稿只会为读过并填了矩阵的文献写出具体论断。`,
    },
    {
      key: 'matrix',
      label: '待填精读矩阵',
      value: noMatrix,
      unit: '篇',
      level: noMatrix === 0 ? 'ok' : noMatrix <= n * 0.5 ? 'warn' : 'bad',
      detail: '方法 + 结论两项都空的条目，在综述里只能以待读占位出现。',
    },
    {
      key: 'fields',
      label: '缺摘要 / 缺链接',
      value: `${noAbstract} / ${noId}`,
      unit: '篇',
      level: noAbstract <= n * 0.2 && noId <= n * 0.3 ? 'ok' : 'warn',
      detail: '摘要缺失会影响主题聚类的准确度；DOI 或链接缺失会让参考文献著录不完整。',
    },
    {
      key: 'author',
      label: '第一作者集中度',
      value: Math.round(concentration * 100),
      unit: '%',
      level: concentration <= 0.2 ? 'ok' : 'warn',
      detail: topAuthor ? `出现最多的第一作者是 ${topAuthor[0]}（${topAuthor[1]} 篇）。同一课题组占比过高时，注意别把一家之言当作领域共识。` : '暂无作者信息。',
    },
  ];
}
