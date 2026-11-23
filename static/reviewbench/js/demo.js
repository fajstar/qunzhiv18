/* 演示文献库 —— 全部条目为虚构，作者名、DOI、期刊名均为占位内容，
   仅用于让评委在 30 秒内看到聚类、矩阵、查重与综述骨架的真实效果。
   导入你自己的 .bib 后会被整体替换。 */

import { blankItem } from './parsers.js';

const RAW = [
  // A 组：自动作文评分
  {
    title: 'Large Language Models as Reliable Graders? A Calibration Study of Automated Essay Scoring',
    authors: ['M. Halloran', 'P. Nascimento', 'Y. Qiu'], year: 2025, venue: 'Journal of Learning Analytics', type: 'journal',
    doi: '10.9999/reviewbench.demo.01',
    keywords: ['essay scoring', 'calibration', 'human agreement', 'rubric'],
    abstract: 'We compare seven instruction-tuned language models against two human raters on 4,180 argumentative essays written by first-year students. Model-based scores agree with humans at quadratic weighted kappa 0.71, but agreement collapses on rubric dimensions that require judging the novelty of an argument. We introduce a calibration step using a small human-graded anchor set, which reduces systematic leniency by 34 percent.',
    status: 'done',
    matrix: { method: '七个指令微调模型对 4180 篇议论文打分，与两位人工评分者做一致性比较', data: '4180 篇一年级学生议论文 + 双人工评分', findings: '整体 QWK 0.71，但在「论证新颖性」维度一致性显著下降；用少量人工锚点校准后系统性偏松减少 34%', limits: '单一校单一文体，跨校迁移和评分者性别/母语效应未检验' },
  },
  {
    title: 'Rubric-Aware Prompting Improves Cross-Course Transfer of LLM Grading',
    authors: ['L. Ferrante', 'S. Okonkwo'], year: 2026, venue: 'Proceedings of AIED', type: 'conference',
    doi: '10.9999/reviewbench.demo.02',
    keywords: ['essay scoring', 'rubric', 'transfer', 'prompting'],
    abstract: 'Grading policies written as structured rubrics are injected into the prompt to make model scores transfer across courses. On 11 courses from two departments, rubric-aware prompting cuts the number of human-graded calibration examples needed per course from 120 to 22. Errors concentrate on essays that mix disciplines.',
    status: 'reading',
    matrix: { method: '把结构化评分细则写入提示词，跨 11 门课程测试迁移', findings: '每门课所需人工校准样本从 120 降到 22' },
  },
  {
    title: 'Student Perceptions of Algorithmic Grading: Fairness, Opacity and Appeal',
    authors: ['H. Braun', 'R. Costello', 'T. Ito'], year: 2024, venue: 'Assessment & Evaluation in Higher Education', type: 'journal',
    doi: '10.9999/reviewbench.demo.03',
    keywords: ['essay scoring', 'fairness', 'student perception', 'appeal'],
    abstract: 'Survey and 42 interviews with students whose essays were graded by an AI system that also allowed appeals. Students accept machine scoring when the rubric is published and when an appeal route exists, but perceive the process as unfair when feedback is generic. Trust correlates with feedback specificity, not with score accuracy.',
    status: 'done',
    matrix: { method: '问卷加 42 名学生的半结构访谈', data: '一门通识写作课两个学期的 AI 评分与申诉记录', findings: '学生接受度取决于细则公开与申诉通道；信任与反馈具体程度相关，而与分数准确性关系弱', limits: '样本来自单门课程，申诉成功率低导致申诉者视角样本极小' },
  },
  {
    title: 'Bias in LLM-Based Writing Assessment: Dialect, Length and Native-Speaker Effects',
    authors: ['K. Adeyemi', 'M. Halloran'], year: 2025, venue: 'Journal of Educational Data Mining', type: 'journal',
    doi: '10.9999/reviewbench.demo.04',
    keywords: ['essay scoring', 'bias', 'dialect', 'fairness'],
    abstract: 'Controlled experiments that paraphrase the same essay into three English dialects and two lengths. Model scores shift by up to 0.8 band levels for non-standard dialects even when content is identical. Length correlates with score more strongly in models than in human raters.',
    status: 'todo',
  },
  {
    title: '评分反馈的具体性如何影响学习投入：来自写作课的实证',
    authors: ['陈慕熙', '李若谷'], year: 2024, venue: '现代教育技术', type: 'journal',
    doi: '10.9999/reviewbench.demo.05',
    keywords: ['作文评分', '反馈', '学习投入', '写作课'],
    abstract: '以两学期大学英语写作课的机器反馈文本为材料，考察反馈具体性与学生修改行为的关系。研究显示指向段落的反馈带来平均 2.3 轮修改，泛泛表扬类反馈仅 0.6 轮，且学生的求助意愿显著下降。研究提示自动评分的价值主要在反馈而非分数。',
    status: 'todo',
  },

  // B 组：幻觉与引用可靠性
  {
    title: 'Fabricated Citations in Generative Research Assistants: Measurement and Mitigation',
    authors: ['D. Whitfield', 'A. Sorokina'], year: 2025, venue: 'Proceedings of the ACM on Human-Computer Interaction', type: 'conference',
    doi: '10.9999/reviewbench.demo.06',
    keywords: ['hallucination', 'citation', 'retrieval', 'research assistant'],
    abstract: 'We audit 2,900 references produced by four research-assistant tools answering real literature-search requests from graduate students. 17 percent of citations are unresolvable, and fabrication concentrates on recent venue names. Grounding every claim in a retrieved record plus a post-hoc verifier lowers the unresolvable rate to 3 percent at the cost of refusing to answer 21 percent of requests.',
    status: 'done',
    matrix: { method: '审计四种科研助手工具产生的 2900 条引用', data: '研究生真实文献检索请求日志', findings: '17% 引用无法解析；强制引用可回溯并加入校验器后降到 3%，代价是 21% 的请求被拒绝回答', limits: '仅覆盖英文文献库，未评估语义正确但来源错误的引用' },
  },
  {
    title: 'Hallucination Rates in Domain-QA Systems for Undergraduates: A Replication',
    authors: ['J. Marchetti', 'N. Balakrishnan'], year: 2024, venue: 'Computers & Education', type: 'journal',
    doi: '10.9999/reviewbench.demo.07',
    keywords: ['hallucination', 'question answering', 'undergraduate'],
    abstract: 'Replication of a widely cited hallucination benchmark using course materials from four disciplines. Contrary to the original study, unanswerable questions are answered confidently in 44 percent of trials, and the rate rises when the system is told to be helpful. Calibration by verbalized confidence reduces overconfident errors but degrades student satisfaction.',
    status: 'reading',
    matrix: { findings: '无解问题被自信作答的比例 44%，且被要求「有用」时更高' },
  },
  {
    title: 'Grounding Course Chatbots in Syllabi and Slide Decks Reduces Invented Policies',
    authors: ['E. Villanueva', 'C. Rhee'], year: 2026, venue: 'International Journal of AI in Education', type: 'journal',
    doi: '10.9999/reviewbench.demo.08',
    keywords: ['hallucination', 'course chatbot', 'grounding', 'slides'],
    abstract: 'A course chatbot restricted to syllabus, slides and assignment sheets produced 6 percent invented policy statements versus 29 percent for an unrestricted model. The gains disappear for questions that require combining a policy with a calendar, suggesting the need for structured reasoning over documents rather than plain retrieval.',
    status: 'todo',
  },
  {
    title: '学生能否识别语言模型生成的错误解释？一个对照实验',
    authors: ['黄予涵', '周景明'], year: 2025, venue: '中国电化教育', type: 'journal',
    doi: '10.9999/reviewbench.demo.09',
    keywords: ['幻觉', '错误解释', '批判性审查', '对照实验'],
    abstract: '向两个专业的本科生呈现含错误的模型解释文本与等价的人工错误文本，测量检出率。学生对模型错误的检出率低于人工错误约 12 个百分点，且高年级差异更大。提示错误存在可以缩小差距，但会同时降低其对正确内容的采纳。',
    status: 'todo',
  },

  // C 组：课程知识库 / RAG
  {
    title: 'Retrieval over Personal Reading Notes Produces Better Literature Summaries than Fine-Tuning',
    authors: ['A. Sorokina', 'F. Duval', 'G. Meisen'], year: 2026, venue: 'Proceedings of EMNLP (education track)', type: 'conference',
    doi: '10.9999/reviewbench.demo.10',
    keywords: ['retrieval augmented', 'personal notes', 'summarization', 'literature review'],
    abstract: 'Graduate students kept structured reading notes for nine weeks. A retrieval pipeline over those notes generated literature summaries that peers rated as more faithful and more useful than summaries from a domain fine-tuned model, at a fraction of the compute. The advantage depends on note quality, which the authors measure with a per-entry completeness score.',
    status: 'done',
    matrix: { method: '在九周结构化阅读笔记上搭建检索管线生成综述摘要，与领域微调模型对比', data: '14 名研究生的阅读笔记库', findings: '同伴评估认为检索方案生成的摘要更忠实、更有用，且算力开销远低于微调', limits: '优势依赖笔记质量，笔记本身参差会影响结论可迁移性' },
  },
  {
    title: 'Indexing Lecture Slides for Question Answering: Chart, Formula and Speaker-Note Handling',
    authors: ['C. Rhee', 'V. Osborne'], year: 2025, venue: 'Proceedings of LAK', type: 'conference',
    doi: '10.9999/reviewbench.demo.11',
    keywords: ['lecture slides', 'multimodal', 'retrieval', 'formula'],
    abstract: 'Slide decks are indexed with separate paths for body text, embedded charts, formulas and speaker notes. Retrieval accuracy on student questions is 22 points lower on chart-derived facts than on text facts. Converting charts into caption plus extracted data points recovers most of the gap.',
    status: 'reading',
    matrix: { data: '三门理工课程的 214 张幻灯片' },
  },
  {
    title: 'A Course Knowledge Base Built by Students: Crowdsourcing Retrieval Corpora',
    authors: ['T. Ito', 'S. Okonkwo'], year: 2023, venue: 'Journal of Computer Assisted Learning', type: 'journal', doi: '',
    url: 'https://example.org/reviewbench/crowdsourced-corpus',
    keywords: ['knowledge base', 'crowdsourcing', 'retrieval', 'course'],
    abstract: 'Students co-author the retrieval corpus their course assistant uses, under a review workflow. Contributions improve corpus coverage of exam-relevant topics, and the act of writing entries itself correlates with quiz performance. Maintaining the corpus becomes the main cost after three offerings.',
    status: 'todo',
  },
  {
    title: '课件知识库的检索粒度：段落级还是问题级？',
    authors: ['郭思远'], year: 2024, venue: '远程教育杂志', type: 'journal', doi: '',
    keywords: ['知识库', '检索粒度', '课件', '问答'],
    abstract: '比较以段落、幻灯片页与问题三种粒度索引课件文本时的回答质量。问题级索引在事实型问题上最优，段落级在需要跨页推理的问题上更稳定。作者建议按问题类型路由到不同粒度的索引。',
    status: 'todo',
  },

  // D 组：学术诚信与检测
  {
    title: 'AI Text Detectors on Student Writing: False Positives Against Non-Native Speakers',
    authors: ['K. Adeyemi', 'H. Braun'], year: 2024, venue: 'Proceedings of FAccT', type: 'conference',
    doi: '10.9999/reviewbench.demo.12',
    keywords: ['detection', 'academic integrity', 'false positive', 'non-native'],
    abstract: 'Six commercial detectors are evaluated on 1,240 essays with known authorship. Overall accuracy looks acceptable, but non-native speakers are falsely flagged two to four times more often, and a single rewrite of a human essay flips most flags. The authors argue detector output should not be used as evidence in disciplinary procedures.',
    status: 'done',
    matrix: { method: '评估六个商用 AI 文本检测器', data: '1240 篇作者身份已知的学生作文', findings: '非母语学生被误判率为母语的 2 到 4 倍；人工作文经一次改写即多数被标记为 AI 生成', limits: '检测器版本更新快，结论有时效性；未获得检测器内部阈值' },
  },
  {
    title: 'Detection-Resistant Rewriting: How Easily Can Paraphrasing Evade Classroom Detectors?',
    authors: ['P. Nascimento', 'D. Whitfield'], year: 2025, venue: 'Journal of Academic Ethics', type: 'journal',
    doi: '10.9999/reviewbench.demo.13',
    keywords: ['detection', 'paraphrasing', 'academic integrity'],
    abstract: 'Model-generated essays passed as human in 78 percent of cases after applying two common paraphrasing transformations, while the semantic content stayed intact. Detector scores correlate poorly with the presence of a machine in the writing pipeline.',
    status: 'todo',
  },
  {
    title: 'Assessment Design under Pervasive Generative AI: Faculty Strategies and Trade-offs',
    authors: ['R. Costello', 'L. Ferrante', 'J. Marchetti'], year: 2026, venue: 'Higher Education Research & Development', type: 'journal',
    doi: '10.9999/reviewbench.demo.14',
    keywords: ['assessment design', 'academic integrity', 'faculty', 'policy'],
    abstract: 'Interviews with 38 faculty who redesigned assessments after generative AI became routine: oral defences, in-class drafting with version history, and assessments whose value comes from process artifacts. Most report increased grading workload, and equity concerns arise when process evidence favours students with more time.',
    status: 'reading',
    matrix: { method: '对 38 名教师做访谈编码' },
  },
  {
    title: '生成式 AI 时代高校学术诚信教育的课程嵌入路径',
    authors: ['吴知远', '陈慕熙'], year: 2023, venue: '复旦教育论坛', type: 'journal', doi: '',
    keywords: ['学术诚信', '课程嵌入', 'AI 使用规范', '高校'],
    abstract: '分析九所高校的 AI 使用规范文本与课程大纲，归纳三种诚信教育嵌入路径：规则宣贯型、任务重塑型与能力本位型。规则宣贯型落地最快但学生知晓率低，任务重塑型与课程质量关联最强。',
    status: 'todo',
  },

  // E 组：智能辅导与反馈对话
  {
    title: 'Socratic or Direct? Feedback Style Effects in LLM Tutoring for Introductory Physics',
    authors: ['N. Balakrishnan', 'E. Villanueva'], year: 2025, venue: 'International Journal of Computer-Supported Learning', type: 'journal',
    doi: '10.9999/reviewbench.demo.15',
    keywords: ['tutoring', 'feedback style', 'physics', 'dialogue'],
    abstract: 'A randomized comparison of Socratic prompting and direct explanation in an LLM tutor used for six weeks by 612 students. Direct explanation yields higher immediate homework scores, Socratic tutoring yields higher delayed test scores and more self-explanation. Effects are strongest for students with prior knowledge.',
    status: 'done',
    matrix: { method: '随机对照，苏格拉底式提问 vs 直接讲解两种反馈风格', data: '612 名普通物理课程学生使用六周', findings: '直接讲解提高当次作业分数，苏格拉底式提高延后测验分数与自我解释频次；先验知识强的学生获益最大', limits: '单校单课程，未测量长期使用成本与教师参与度' },
  },
  {
    title: 'When the Tutor Guesses: Recovering from Wrong Hints in Multi-Turn Dialogue',
    authors: ['F. Duval', 'G. Meisen'], year: 2026, venue: 'Proceedings of AIED', type: 'conference', doi: '',
    keywords: ['tutoring', 'dialogue', 'error recovery', 'hints'],
    abstract: 'In 3,400 logged tutoring dialogues, students rarely challenge an incorrect hint; instead they abandon the problem. Explicit uncertainty markers and a one-click dispute affordance raise the correction rate from 8 percent to 31 percent. Recovery quality depends on whether the system keeps the student’s prior answers in context.',
    status: 'todo',
  },
  {
    title: 'Tutoring in the Mother Tongue, Coding in English: Bilingual LLM Support for CS Homework',
    authors: ['V. Osborne', '黄予涵'], year: 2024, venue: 'Computer Science Education', type: 'journal',
    doi: '10.9999/reviewbench.demo.16',
    keywords: ['bilingual', 'tutoring', 'programming', 'cs education'],
    abstract: 'Bilingual tutoring (explanations in the local language, code and errors in English) reduces time-on-task for introductory programming homework by 19 percent relative to an English-only assistant, with no loss in assignment grades. Students request bilingual explanations mainly for error messages.',
    status: 'reading',
    matrix: { findings: '相比纯英文助手，双语讲解使编程作业用时下降 19% 且成绩无损' },
  },
  {
    title: '教师如何使用课堂助手：一位数教师的两个月日志研究',
    authors: ['周景明'], year: 2025, venue: '电化教育研究', type: 'journal', doi: '',
    keywords: ['智能辅导', '教师实践', '日志研究', '课堂助手'],
    abstract: '通过两个月的教师日志与三次访谈，刻画一线教师使用课堂助手的实践轨迹。教师把助手主要用于题目改编与差异化材料生成，而非直接面向学生答疑；对生成内容的手工改写比例逐周下降，作者提醒注意审查松弛风险。',
    status: 'todo',
  },

  // F 组：无障碍 / 技术向善
  {
    title: 'Screen-Reader Friendly Explanations: Structuring LLM Output for Blind Students',
    authors: ['A. Whitfield', 'C. Rhee', 'K. Adeyemi'], year: 2026, venue: 'ASSETS', type: 'conference',
    doi: '10.9999/reviewbench.demo.17',
    keywords: ['accessibility', 'screen reader', 'blind students', 'output structure'],
    abstract: 'Blind students using a chat assistant spend most of their time navigating tables, nested lists and decorative formatting. Requiring flat, ordered, prose-first output cuts navigation time by half in a within-subjects study with nine participants and is preferred by all of them.',
    status: 'done',
    matrix: { method: '被试内设计，对比自由格式与扁平散文式输出的屏幕阅读器使用时长', data: '9 名视障大学生', findings: '要求扁平、有序、散文优先的输出使导航时间减半，9 名被试全部偏好该格式', limits: '样本仅 9 人，任务局限于单一学科材料' },
  },
  {
    title: 'Speech-Only Interfaces for Elderly Learners: Bridging the Digital Divide in Online Courses',
    authors: ['T. Ito', 'H. Braun'], year: 2024, venue: 'Geriatrics & Gerontology International', type: 'journal', doi: '',
    keywords: ['elderly', 'digital divide', 'speech interface', 'online course'],
    abstract: 'Community-dwelling adults aged 65 and above learned a smartphone course through either a typing or a speech-only interface. Speech-only doubled completion rates and reduced abandonment at the login step, but misrecognition of dialect caused frustration that required a human fallback channel.',
    status: 'todo',
  },
  {
    title: '为乡村教师生成差异化教案：一套低带宽工具的设计与试用',
    authors: ['郭思远', '李若谷', '吴知远'], year: 2025, venue: '中国特殊教育', type: 'journal', doi: '',
    keywords: ['乡村教育', '教案生成', '低带宽', '差异化教学'],
    abstract: '面向县域初中教师设计可离线使用的教案生成工具，采用小模型本地推理与增量同步以适配低带宽环境。在三所乡镇中学试用十周，教师备课时间平均下降 41 分钟，但差异化练习的可用性评分明显低于讲解部分。',
    status: 'todo',
  },

  // G 组：多模态与实验数据处理
  {
    title: 'Multimodal Understanding of Chemistry Lab Reports with Figures and Spectra',
    authors: ['J. Marchetti', 'V. Osborne', 'N. Balakrishnan'], year: 2026, venue: 'Journal of Chemical Education', type: 'journal',
    doi: '10.9999/reviewbench.demo.18',
    keywords: ['multimodal', 'lab report', 'figures', 'spectra', 'chemistry'],
    abstract: 'A vision-language model is evaluated on grading undergraduate chemistry lab reports containing spectra and hand-drawn figures. It matches human scores on conclusion sections but fails on figure interpretation, often accepting spectra inconsistent with the claimed result.',
    status: 'todo',
  },
  {
    title: 'From Lab Notebook to Draft: Structured Extraction of Experimental Results',
    authors: ['G. Meisen', 'S. Okonkwo'], year: 2025, venue: 'Proceedings of the ACM on Software Engineering?', type: 'journal', doi: '',
    keywords: ['experiment data', 'extraction', 'notebook', 'data processing'],
    abstract: 'Undergraduate lab notebooks are parsed into structured records of apparatus, parameters, and outcomes, which then feed a template for the results section. Errors concentrate on unit conversion and on handwritten tables, both handled by asking the student to confirm rather than by trusting extraction.',
    status: 'reading',
    matrix: { method: '把实验记录本解析为仪器、参数与结果的结构化记录' },
  },

  // 近重复示例：预印本与正式发表版（用于演示查重）
  {
    title: 'Large Language Models as Reliable Graders: A Calibration Study of Automated Essay Scoring',
    authors: ['M. Halloran', 'P. Nascimento', 'Y. Qiu'], year: 2024, venue: 'arXiv preprint (demo)', type: 'preprint', doi: '',
    url: 'https://example.org/reviewbench/preprint-01',
    keywords: ['essay scoring', 'calibration'],
    abstract: 'Preprint version of a calibration study comparing instruction-tuned language models with human raters on automated essay scoring. Agreement is high overall but weak for novelty judgements; anchor-based calibration reduces leniency.',
    status: 'todo',
  },
];

export function demoItems() {
  return RAW.map((r) => blankItem({ ...r, demo: true }));
}
