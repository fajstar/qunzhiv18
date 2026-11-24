#!/usr/bin/env node
/* 群智 local · 零依赖自测
 * 运行：node tests/selftest.js
 * 不联网、不需要 npm；直接加载 static/rules.js 与 static/anchor.js。
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "..");
["static/rules.js", "static/anchor.js"].forEach(function (f) {
  vm.runInThisContext(fs.readFileSync(path.join(root, f), "utf8"), { filename: f });
});

const R = globalThis.QZ_RULES;
const A = globalThis.QZ_ANCHOR;
let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log("PASS  " + name); }
  else { fail++; console.log("FAIL  " + name + (detail ? "  ← " + detail : "")); }
}

// ---- 1. 规则清单 ----
ok("20 条规则", R.RULES.length === 20, "实际 " + R.RULES.length);
ok("20 个 feat", R.FEATS.length === 20, "实际 " + R.FEATS.length);
ok("规则 id 与 feat 名单一致",
  R.FEATS.every(function (f) { return !!R.byId(f); }) &&
  R.RULES.every(function (r) { return R.FEATS.indexOf(r.id) >= 0; }));

// ---- 2. 规则检测 ----
const TEXT = [
  "Title: A Better Method",
  "",
  "1. Introduction",
  "We propose a new method and show it may improve accuracy. Our contribution is a new module.",
  "",
  "3. Experiments",
  "We use three random seeds (42, 43, 44) and report the standard deviation. We train on A100 for 96 GPU hours. The code is available at github.com/example/demo. We compare against baselines methods. We run an ablation study on the router. We use a paired t-test with p = 0.03. The dataset contains 1,281,167 images in the training split.",
  "",
  "5. Limitations",
  "Our method fails on small datasets and we do not test detection.",
  "",
  "References",
  "[1] Author A, Author B. Deep Residual Learning for Image Recognition. In CVPR, 2016.",
  "[2] Author C. Attention Is All You Need. In NeurIPS, 2017.",
  "[3] Author D. BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding. In NAACL, 2019."
].join("\n");

const split = A.splitReferences(TEXT);
ok("识别 References 段", split.found === true);
ok("正文不含参考文献标题", split.body.indexOf("Deep Residual Learning") < 0);
ok("提取 3 条参考文献", split.refs.length === 3, JSON.stringify(split.refs));
ok("参考文献标题 1 正确", split.refs[0] === "Deep Residual Learning for Image Recognition", split.refs[0]);
ok("参考文献标题 3 正确", split.refs[2].indexOf("BERT") === 0, split.refs[2]);

["seed", "ci", "code", "baseline_table", "ablation", "significance", "compute_budget", "dataset_stats", "limitations", "claim_hedging", "novelty_statement"].forEach(function (id) {
  const hits = R.detect(R.byId(id), split.body, 2);
  ok("规则命中 " + id, hits.length > 0);
});
ok("规则不误报 cross_val", R.detect(R.byId("cross_val"), split.body, 2).length === 0);

// ---- 3. 锚定 ----
const good = "This sentence is deliberately made long enough to pass the forty character threshold for T1.";
const an1 = A.anchorQuote("Intro. " + good + " Next sentence.", good);
ok("T1 逐字命中", an1.level === "T1", JSON.stringify(an1));
ok("T1 带行号", an1.line === 1, String(an1.line));

const an2 = A.anchorQuote(TEXT, "We run an ablation study on the router.");
ok("短句只能是 T2", an2.level === "T2", JSON.stringify(an2));

const an3 = A.anchorQuote(TEXT, "This sentence does not exist in the paper at all.");
ok("找不到即 T3", an3.level === "T3", JSON.stringify(an3));

const an4 = A.anchorQuote(TEXT, "The code is available at github.com/example/demo.");
ok("域名不会被句号截断", an4.quote.indexOf("github.com/example/demo") > 0, an4.quote);

const sec = A.anchorSection(TEXT, ["limitations"]);
ok("章节线索 T2", sec.level === "T2" && sec.line === 9, JSON.stringify(sec));

const st = A.stats([an1, an2, an3]);
ok("锚定统计", st.T1 === 1 && st.T2 === 1 && st.T3 === 1, JSON.stringify(st));

// ---- 4. 句子边界 ----
const abbr = "We use e.g. this example. Then another sentence that is long enough to be quoted as evidence.";
const an5 = A.anchorQuote(abbr, "Then another sentence that is long enough to be quoted as evidence.");
ok("e.g. 不误切句", an5.level === "T1" && an5.quote.indexOf("e.g.") < 0, JSON.stringify(an5));

console.log("\n" + pass + " passed, " + fail + " failed");
if (fail) process.exit(1);
