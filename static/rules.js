/* 群智 local · 20 项规则引擎
 * ---------------------------------------------------------------
 * 与后端 paper_features 的 20 个 feat 严格同名：
 *   seed, ci, ablation, hyperparameter, dataset_stats, code,
 *   sota_compare, baseline_table, related_work_table, human_eval,
 *   multiple_runs, cross_val, threat_validity, ethics, limitations,
 *   novelty_statement, claim_hedging, compute_budget, released_data,
 *   significance
 *
 * 正则取自后端 features_extract_v2.py（保持口径一致），只做两处调整：
 *   1. JS 不支持内联 (?i)，统一用 /i；
 *   2. 后端把 20 项压成一行行文本，这里补上「为什么重要 / 怎么改 / 去哪找证据」。
 *
 * 重要边界：正则只能判断「文中是否出现相关表述」，不能判断做得对不对。
 * 命中 = 有线索，需人工确认；未命中 = 未检出，不等于一定没做。
 */
(function (root) {
  "use strict";

  const RULES = [
    {
      id: "seed", name: "随机种子", group: "可复现", severity: "high",
      re: /\brandom(?:ly)?\s+seeds?\b|\bseeds?\s*(?:=|:|\bof\b)|\bseed\s+[0-9]/i,
      why: "不写种子，同一实验无法复现；审稿人也无法判断方差是不是挑出来的。",
      fix: "写明种子数量与范围（如 3 个种子 42/43/44），主表报均值 ± 标准差；附录列每次运行结果。",
      sectionHints: ["experimental setup", "implementation details", "reproducibility", "实验设置", "复现"],
      question: "实验用了哪些随机种子？换一组种子，结论还成立吗？"
    },
    {
      id: "multiple_runs", name: "多次运行", group: "可复现", severity: "high",
      re: /multiple runs|repeated (?:runs|experiments)|average(?:d)? over \d+ runs/i,
      why: "单次结果可能只是幸运样本；深度学习训练的随机性足以改变排名。",
      fix: "至少 3 次独立运行，报告均值、标准差与每次结果；不要只报最好的一次。",
      sectionHints: ["experiments", "results", "experimental setup", "实验", "结果"],
      question: "每个配置跑了几次？表里的数字是单次最好，还是多次平均？"
    },
    {
      id: "ci", name: "置信区间 / 误差棒", group: "统计严谨", severity: "high",
      re: /confidence interval|error bars?|standard deviation|\bstd\.?\b|±\s*[0-9]/i,
      why: "只给点估计，读者无法判断提升是否大于噪声。",
      fix: "主表给 std 或 95% CI，并说明样本量 n；图上的误差棒注明是 std、SEM 还是 CI。",
      sectionHints: ["results", "experiments", "analysis", "结果", "分析"],
      question: "表里的 ± 是什么？样本量多少？提升超过误差范围了吗？"
    },
    {
      id: "significance", name: "显著性检验", group: "统计严谨", severity: "medium",
      re: /significan(?:t|ce)|p-?value|statistical test|wilcoxon|t-test/i,
      why: "没有检验，0.3 个点的提升可能只是随机波动。",
      fix: "配对检验（如 Wilcoxon / 配对 t），报 p 值与效应量；多次比较要做校正。",
      sectionHints: ["results", "statistical analysis", "实验", "显著性"],
      question: "关键提升做过显著性检验吗？p 值和效应量是多少？"
    },
    {
      id: "ablation", name: "消融实验", group: "统计严谨", severity: "high",
      re: /\bablation/i,
      why: "不做消融，无法判断到底是哪个组件在起作用。",
      fix: "逐组件移除/替换，保持数据、预算、超参搜索一致；报告每个组件的边际贡献。",
      sectionHints: ["ablation", "analysis", "消融", "分析"],
      question: "每个新组件单独拿掉后，指标掉多少？"
    },
    {
      id: "baseline_table", name: "强基线对比", group: "统计严谨", severity: "high",
      re: /table\s+\d+[^\n]{0,80}baseline|baseline[s]?\s+(?:methods?|models?|systems?)/i,
      why: "只跟弱基线比，提升可能来自训练预算而不是方法。",
      fix: "同数据、同预算、同评测脚本下比较 2–3 个强基线；注明基线的调参预算与来源。",
      sectionHints: ["baselines", "comparison", "experiments", "基线", "对比"],
      question: "最强基线是谁？它的调参预算和你一样吗？"
    },
    {
      id: "sota_compare", name: "SOTA 对比", group: "统计严谨", severity: "medium",
      re: /state[- ]of[- ]the[- ]art|\bsota\b|strongest baseline/i,
      why: "不引最近的最强结果，读者会高估进展。",
      fix: "列出最近 12 个月的 SOTA，注明数字是原论文报告还是你复现的。",
      sectionHints: ["related work", "comparison", "相关工作", "对比"],
      question: "最近 12 个月的最强结果引了吗？数字来源是哪篇？"
    },
    {
      id: "dataset_stats", name: "数据统计", group: "统计严谨", severity: "medium",
      re: /\b(?:number of|# of)\s+(?:training|samples|instances|examples)|\b\d[\d,]{2,}\s+(?:samples|instances|images|documents)/i,
      why: "数据规模、切分、去重不清，无法判断泄漏与可比性。",
      fix: "给出样本数、训练/验证/测试切分、去重规则、标注一致性；说明是否与基线同一版本。",
      sectionHints: ["dataset", "data", "experimental setup", "数据集", "数据"],
      question: "训练/测试各多少条？怎么切的？和基线用同一版本吗？"
    },
    {
      id: "hyperparameter", name: "超参数", group: "可复现", severity: "medium",
      re: /hyper-?parameters?|learning rate|batch size|grid search/i,
      why: "没有完整超参与搜索空间，别人无法复现，也无法判断调参公平性。",
      fix: "附录表列出搜索范围、选择方法（网格/贝叶斯/默认值）、最终值；说明是否对基线同样调参。",
      sectionHints: ["implementation details", "experimental setup", "hyperparameters", "实现细节", "超参"],
      question: "超参搜索空间多大？基线享受了同样的搜索预算吗？"
    },
    {
      id: "compute_budget", name: "算力预算", group: "可复现", severity: "medium",
      re: /\bgpu[- ]?hours?\b|compute budget|training time|a100|v100|h100|tpu/i,
      why: "不算力，无法判断比较是否公平，也无法估算复现成本。",
      fix: "写 GPU 型号 × 卡数 × 小时、总能耗/成本；推理任务补延迟与吞吐。",
      sectionHints: ["experimental setup", "implementation details", "compute", "算力", "训练"],
      question: "训练一共花了多少 GPU 小时？基线也是同样预算吗？"
    },
    {
      id: "code", name: "代码可得", group: "可复现", severity: "high",
      re: /github\.com|code\s+(?:is\s+)?(?:publicly\s+)?available|open[- ]sourced?/i,
      why: "代码是复现的核心证据；只写「可提供」通常等于拿不到。",
      fix: "给匿名仓库或永久链接（Zenodo/Figshare），附环境文件、运行脚本、许可证。",
      sectionHints: ["reproducibility", "code availability", "appendix", "代码", "复现"],
      question: "代码在哪？评审阶段能匿名访问吗？有运行脚本和许可证吗？"
    },
    {
      id: "released_data", name: "数据释放", group: "可复现", severity: "medium",
      re: /dataset[s]?\s+(?:is\s+)?(?:publicly\s+)?(?:available|released)|we release/i,
      why: "数据不可得，结果就无法独立复核。",
      fix: "给下载/申请方式、许可证、脱敏说明；不能公开时说明原因与申请流程。",
      sectionHints: ["data availability", "dataset", "appendix", "数据", "释放"],
      question: "数据能拿到吗？许可证是什么？不能公开的部分怎么申请？"
    },
    {
      id: "cross_val", name: "交叉验证", group: "可复现", severity: "low",
      re: /cross[- ]validation|\bk-?fold\b/i,
      why: "小数据单次切分方差大，结论可能依赖某次划分。",
      fix: "k-fold 重复多次，报告折间均值与方差；说明是否分层、是否按作者/时间切分防泄漏。",
      sectionHints: ["experimental setup", "evaluation", "实验", "评测"],
      question: "小数据场景做交叉验证了吗？折间方差多大？"
    },
    {
      id: "human_eval", name: "人工评估", group: "评审透明", severity: "medium",
      re: /human evaluation|human study|annotators?|crowdworkers?|irb/i,
      why: "主观指标没有评估协议，数字无法解释。",
      fix: "写评估者数量与背景、标注指南、一致性（κ/α）、报酬、盲评方式。",
      sectionHints: ["human evaluation", "annotation", "人工评估", "标注"],
      question: "谁评的？多少人？一致性多少？评估者知道方法身份吗？"
    },
    {
      id: "ethics", name: "伦理声明", group: "评审透明", severity: "medium",
      re: /\bethic|consent|privacy|bias(?:es)?\s+(?:in|of)\b/i,
      why: "涉及人类数据、隐私或偏见时，缺声明会被直接质疑合规性。",
      fix: "写 IRB/知情同意、数据许可、隐私处理、潜在偏见与缓解措施。",
      sectionHints: ["ethics", "broader impact", "privacy", "伦理", "影响"],
      question: "涉及人类数据/隐私/偏见吗？伦理审批号是多少？"
    },
    {
      id: "limitations", name: "局限性", group: "评审透明", severity: "high",
      re: /\blimitations?\b|future work/i,
      why: "不写边界，读者会把结论套到不适用的场景。",
      fix: "明确失败模式、数据域、最差情况、负结果；不要只写「未来我们会更好」。",
      sectionHints: ["limitations", "discussion", "threats to validity", "局限", "讨论"],
      question: "这个方法在什么情况下会失效？有没有负结果没报？"
    },
    {
      id: "threat_validity", name: "效度威胁", group: "评审透明", severity: "medium",
      re: /threats? to validity|external validity|internal validity/i,
      why: "审稿人关心结论能否推广，以及评测是否测到了想测的东西。",
      fix: "分内部/外部/构造/统计效度讨论，并说明缓解措施。",
      sectionHints: ["threats to validity", "validity", "discussion", "效度", "讨论"],
      question: "结论能推广到哪些数据/场景？评测指标真的测到了目标能力吗？"
    },
    {
      id: "related_work_table", name: "相关工作对比表", group: "评审透明", severity: "low",
      re: /related work|comparison table/i,
      why: "文字综述容易漏掉关键差异，读者无法快速定位贡献。",
      fix: "加一张表：方法 / 数据 / 指标 / 关键假设 / 与本文差异。",
      sectionHints: ["related work", "相关工作"],
      question: "和最近的三篇最相关工作，具体差异是什么？"
    },
    {
      id: "novelty_statement", name: "贡献声明", group: "表述", severity: "medium",
      re: /our contribution|to the best of our knowledge|we are the first/i,
      why: "贡献不清，审稿人无法判断该看哪几个实验。",
      fix: "列 3 条可检验的贡献，每条对应一个实验或一张表；避免「首个」这类无法证伪的说法。",
      sectionHints: ["introduction", "contributions", "引言", "贡献"],
      question: "三条核心贡献分别由哪个实验支撑？"
    },
    {
      id: "claim_hedging", name: "措辞强度", group: "表述", severity: "low",
      re: /\bmay\b|\bmight\b|\bsuggest(?:s|ing)?\b|\bpotentially\b/i,
      why: "「may / suggest」与摘要里的强结论混用，会让读者误读证据强度。",
      fix: "区分观察、相关、因果：摘要与结论用同一强度的措辞，因果结论要有对应实验。",
      sectionHints: ["discussion", "conclusion", "abstract", "讨论", "结论"],
      question: "摘要里的强结论，正文证据支持到哪一层（相关/因果）？"
    }
  ];

  // 与后端 feat 名单对账：缺一项或名字不一致，测试会报错。
  const FEATS = ["seed", "ci", "ablation", "hyperparameter", "dataset_stats",
    "code", "sota_compare", "baseline_table", "related_work_table", "human_eval",
    "multiple_runs", "cross_val", "threat_validity", "ethics", "limitations",
    "novelty_statement", "claim_hedging", "compute_budget", "released_data",
    "significance"];

  /** 在 text 里找一条规则的所有命中（最多 max 条）。 */
  function detect(rule, text, max) {
    const out = [];
    if (!rule || !text) return out;
    const re = new RegExp(rule.re.source, rule.re.flags.includes("g")
      ? rule.re.flags : rule.re.flags + "g");
    let m;
    let guard = 0;
    while ((m = re.exec(text)) !== null && out.length < (max || 5)) {
      if (m[0] === "") { re.lastIndex++; continue; }
      out.push({ index: m.index, match: m[0] });
      if (++guard > 2000) break;
    }
    return out;
  }

  function byId(id) {
    return RULES.find(function (r) { return r.id === id; }) || null;
  }

  root.QZ_RULES = { RULES: RULES, FEATS: FEATS, detect: detect, byId: byId };
})(typeof window !== "undefined" ? window : globalThis);
