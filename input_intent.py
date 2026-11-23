# -*- coding: utf-8 -*-
"""
输入意图识别与需求澄清 input_intent.py
=====================================
作品定位是「决策预演」，那第一步必须保证——**被预演的那个问题本身是明确的**。

现实里用户输入往往还没形成决策问题。例如：
    「想通过小游戏或问答提高课堂参与度」
这是一个**方向**，不是一个可预演的决策问题：没有决策主体、没有时间尺度、
没有成功判据、也没有对照方案。直接丢给 6 个角色，讨论会空转。

本模块只做一件轻量的事（不做动态提示词切换、不扩场景）：
    ① 判断用户是在 提研究问题 / 比较备选方案 / 只有一个初步想法
    ② 指出缺哪些关键条件
    ③ 给一个**规范化的决策问题**供用户确认或直接采用

然后才进入多智能体讨论。

--------------------------------------------------------------------
设计约束
--------------------------------------------------------------------
· 只一次短调用（max_tokens 400），失败就走启发式，绝不阻塞主流程
· 不改变 /api/sandbox/start 的既有行为；本模块是**前置建议**，不是闸门
· 用户永远有权「按原文开始」
"""
import json
import re

INTENT_PROMPT = """你是「决策预演」系统的输入体检员。用户的输入要先能构成一个**明确的决策问题**，
才值得交给 6 个 AI 角色去预演。

请判断用户输入属于哪一类：

A. research_question —— 研究问题（**开放式也算**）
   有明确的研究对象 + 一个可判定的目标（"提升参与度""降低误差""提高立项率"）。
   **「如何…」+ 明确对象 + 可判定目标，就属于 A，即使没说时间尺度和成功判据。**
   缺细节不是缺点：让 6 个角色去探索路径，正是本产品的用途。
   例：「如何提升师范生课堂参与度？」←**判 A，放行**
   例：「学生心理健康筛查与干预体系如何构建？」←**判 A，放行**
   例：「隔音棉厚度对低频噪声衰减的影响有多大？」

B. compare_options —— 备选方案比较
   两个或以上具体方案，要选一个；或明确要平衡两种诉求。
   例：「课题经费该投在买设备还是招研究生？」
   例：「AI 作业该禁止还是引导？成绩和诚信怎么平衡」

C. rough_idea —— 需要澄清（**只在以下两种情形判 C**）
   C1 **目标不可判定**：只有「改革 / 提升 / 优化 / 搞一下」这类没有指标的动词，
      看不出成功长什么样。
      例：「做一下课堂改革」「提高教学质量」「想搞个 AI 的东西」
   C2 **手段已选定，但比较框架未定**：说了要做什么，但没说清是"在几个方案里选"、
      还是"验证这套做法行不行"、和什么比。
      例：「想通过小游戏或问答提高课堂参与度」
         （到底是在小游戏与问答之间选，还是验证这套组合是否优于现状？）
      例：「打算用一个打卡小程序提升出勤率」

  ⚠ 重要：**只要有"明确对象 + 可判定目标"，哪怕用词很朴素，也判 A 而不是 C。**
     不要把"信息不全"当成"不是问题"。

判断 A 必要但不充分的条件，缺得越多越应判 C：
  1) 决策主体：为谁做 / 谁执行
  2) 方案或干预：具体做什么
  3) 时间尺度：多久见效 / 观察多长
  4) 成功判据：怎么算成功（指标）
  5) 对照或约束：和什么比 / 有什么限制

【输出】严格 JSON，不要解释：
{{"intent":"A|B|C","clarity":0到100的整数,"missing":["缺的条件"],"rewritten":"一句规范化的决策问题","reason":"一句话说明"}}

要求：
· rewritten 必须**保留用户原意**，只补全结构，不要换题、不要加入用户没提的方案
· 若 intent 为 A 且 clarity>=75，rewritten 可以就是原问题（或轻微通顺化）
· missing 最多 3 条，只列真正缺失、且补上会显著改变讨论质量的
· 全部用中文

用户课题：{topic}
用户补充的想法：{thought}"""


# ------------------------------------------------------------------
# 启发式兜底（无模型 / 超时 / 解析失败时使用）
# ------------------------------------------------------------------
_CMP = ("该不该", "应不应该", "还是", "哪个", "哪一个", "比较", "对比", "优劣",
        "选哪个", "二选一", "取舍")
_RQ = ("如何", "怎样", "怎么", "能否", "是否", "影响", "效果", "关系",
       "相关性", "差异", "多大", "多少", "为什么")
_CONSTRAINT = ("一学期", "本学期", "半年", "一年", "一个月", "六周", "三个月",
               "周内", "内完成", "预算", "经费", "课时", "人力")
_METRIC = ("参与度", "成绩", "正确率", "提升", "下降", "误差", "满意度",
           "留存", "转化", "产出", "通过率", "有效")


def _heuristic(topic, thought):
    s = (topic or "") + " " + (thought or "")
    t = (topic or "").strip()
    n = len(t)

    missing = []
    has_cmp = any(k in s for k in _CMP)
    has_rq = any(k in s for k in _RQ)
    has_time = any(k in s for k in _CONSTRAINT)
    has_metric = any(k in s for k in _METRIC)
    has_subject = bool(re.search(r"(学生|教师|师范生|研究生|本科生|团队|班级|课题组|患者|用户)", s))

    if not has_subject:
        missing.append("决策主体（为谁做）")
    if not has_time:
        missing.append("时间尺度（多久见效）")
    if not has_metric:
        missing.append("成功判据（怎么算成功）")
    if has_cmp and len(missing) < 3:
        pass

    if has_cmp:
        intent = "B"
        clarity = 72 if (has_metric or has_time) else 58
    elif has_rq and n >= 12:
        intent = "A"
        clarity = 76 if (has_metric and has_time) else (66 if has_metric or has_time else 54)
    else:
        intent = "C"
        clarity = 34 if n < 12 else 44

    if clarity >= 75:
        rewritten = t if t.endswith(("？", "?")) else (t + "？" if t else t)
    else:
        # 用「是否/哪个」结构把「方向」变成「决策问题」，不添加用户没提的方案
        core = t.rstrip("。？！?！ ")
        if intent == "C":
            rewritten = ("面向" + ("目标学生" if not has_subject else "既定对象")
                         + "，用【" + (core or "该手段") + ("】" if core else "")
                         + "，能否在【待定周期】内显著提升【待定指标】？"
                         + "（请补全方括号中的时间尺度与成功判据）")
        else:
            rewritten = core + "（建议补充时间尺度与成功判据后开始预演）"

    # 闸门收紧：只有「初步想法 C」且明确度很低才拦。
    # A（含开放式研究问题）与 B 一律放行——缺细节不是缺点。
    needs_clarify = (intent == "C" and clarity < 45)
    reason = {
        "A": "已构成研究问题，但条件不够完整" if needs_clarify else "已构成明确的研究问题",
        "B": "已给出备选方案，可预演；补充判据后更好" if needs_clarify else "已构成明确的方案比较",
        "C": "目前是方向而非决策问题，缺少可预演的结构",
    }[intent]

    return {
        "intent": intent,
        "clarity": clarity,
        "needs_clarify": needs_clarify,
        "missing": missing[:3],
        "rewritten": rewritten,
        "reason": reason,
        "source": "heuristic",
    }


def _parse(raw, topic, thought):
    """从模型输出里抠 JSON，失败返回 None。"""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group())
    except Exception:
        # 常见：尾逗号、单引号
        try:
            d = json.loads(m.group().replace("'", '"').rstrip(","))
        except Exception:
            return None

    i = str(d.get("intent", "")).strip().upper()[:1]
    if i not in ("A", "B", "C"):
        return None
    try:
        c = int(d.get("clarity", 0))
    except Exception:
        c = 0
    c = max(0, min(100, c))

    miss = d.get("missing") or []
    if isinstance(miss, str):
        miss = [miss]
    miss = [str(x).strip() for x in miss if str(x).strip()][:3]

    rew = str(d.get("rewritten", "")).strip() or (topic or "")
    reason = str(d.get("reason", "")).strip()

    # 与启发式同口径：A/B 放行，只有 C 且明显模糊才拦
    need = (i == "C" and c < 45)
    return {
        "intent": i,
        "clarity": c,
        "needs_clarify": need,
        "missing": miss,
        "rewritten": rew,
        "reason": reason,
        "source": "model",
    }


def assess_intent(topic: str, thought: str = "", api_caller=None, timeout: int = 20):
    """返回 {intent, clarity, needs_clarify, missing, rewritten, reason, source}。永不抛异常。"""
    topic = (topic or "").strip()
    if not topic:
        return {
            "intent": "C", "clarity": 0, "needs_clarify": True,
            "missing": ["决策主体", "时间尺度", "成功判据"],
            "rewritten": "", "reason": "课题为空", "source": "empty",
        }

    if api_caller:
        try:
            r = api_caller(
                INTENT_PROMPT.format(topic=topic, thought=(thought or "（无）")),
                max_tokens=400, temperature=0.1, timeout=timeout,
            )
            d = _parse(r, topic, thought)
            if d:
                return d
        except Exception:
            pass

    return _heuristic(topic, thought)


if __name__ == "__main__":
    for t, th in [
        ("如何提升师范生课堂参与度", ""),
        ("想通过小游戏或问答提高课堂参与度", ""),
        ("AI 作业该禁止还是引导？学生成绩和诚信怎么平衡", ""),
        ("课题经费该买设备还是招研究生，一学期内出成果", ""),
    ]:
        print(t, "→", json.dumps(assess_intent(t, th), ensure_ascii=False))
