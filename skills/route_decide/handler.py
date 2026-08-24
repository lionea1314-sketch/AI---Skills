"""
skills/route_decide/handler.py

S-05 判断该怎么办：按固定顺序把这轮消息分流到六条路之一。

设计要点：
  ① 纯规则零 token，不问模型 —— 路由是整条链路的岔路口，必须可预测、
     可复现、可解释。每个结论都带 reason 和命中的 rule_key。
  ② 完整暴露判定过程 —— evaluated 里连未命中的步骤也记，
     规则调参的人靠它看"为什么没走那条路"（施工规范第 12 条）。
  ③ 单步异常不中断整链 —— 某步规则值坏掉时记 note 后继续下一步，
     最差落到 self_answer，不让路由整个失败。
  ④ 不碰数据库 —— 风险位由 read_view 的第②槽带进来，剧本命中由
     procedure_run 的触发词表带进来，靠角色按序调用传参（第 13 条）。

runtime_context：{org_id, role_id, staff_id, conv_id, rules, db, models}
  本技能只用 rules，不用 db / models。

依赖：无（纯 Python 标准库）
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("myinc.skills.route_decide")

_PATHS = ("self_answer", "kb", "action", "procedure", "appointment", "handoff")

_DEFAULT_ORDER = ("handoff_first", "procedure", "appointment", "action",
                  "kb", "handoff_turns", "fallback")

# 已定义动作的默认意图映射。企业加了新动作，从 defined_actions 传进来覆盖。
_DEFAULT_ACTIONS = ("售后", "会员")

_HUMAN_WORDS = ("人工", "叫人", "转人", "找个人", "真人", "客服经理",
                "主管", "换个人", "不要机器人", "机器人听不懂")
_PRICE_WORDS = ("便宜", "折扣", "打折", "优惠", "降价", "议价", "还价",
                "赔", "补偿", "退多少", "少算", "抹零", "最低价")


def _rule(rules: dict, key: str, default, cast=None):
    raw = (rules or {}).get(key, default)
    if cast is None:
        return raw
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("规则 %s 值非法(%r)，退回默认 %r", key, raw, default)
        return default


def _on(rules: dict, key: str, default: bool) -> bool:
    raw = _rule(rules, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("开", "true", "1", "yes", "on", "启用")


def _order(rules: dict, notes: list) -> tuple:
    """route.order 允许重排，但只认已知步骤名；未知名字丢弃并告警。"""
    raw = _rule(rules, "route.order", None)
    if not raw:
        return _DEFAULT_ORDER
    if isinstance(raw, str):
        raw = [x.strip() for x in re.split(r"[,，\s>→]+", raw) if x.strip()]
    kept = [s for s in raw if s in _DEFAULT_ORDER]
    dropped = [s for s in raw if s not in _DEFAULT_ORDER]
    if dropped:
        logger.warning("route.order 含未知步骤 %r，已忽略", dropped)
        notes.append(f"route.order 含未知步骤 {dropped}，已忽略")
    missing = [s for s in _DEFAULT_ORDER if s not in kept]
    if missing:
        notes.append(f"route.order 未列出 {missing}，按默认顺序追加在末尾")
    return tuple(kept) + tuple(missing)


def _hit(text: str, words) -> str:
    for w in words:
        if w in text:
            return w
    return ""


# --------------------------------------------------------------------------- 七步判定
def _step_handoff_first(ctx: dict, rules: dict) -> tuple:
    """第 1 步：要人工 / 负向情绪 / 涉价。客户说"叫人来"却还要先被 AI 答一轮，是最典型的差评来源。"""
    if _on(rules, "handoff.on_customer_ask", True):
        if ctx["asked_human"] or _hit(ctx["text"], _HUMAN_WORDS):
            hit = _hit(ctx["text"], _HUMAN_WORDS) or "input.customer_asked_human"
            return "handoff", f"客户明确要人工（命中 {hit}）", "handoff.on_customer_ask"
    if _on(rules, "handoff.on_negative", True) and ctx["emotion"] == "负向":
        return "handoff", "情绪判为负向，第一时间转人", "handoff.on_negative"
    if _on(rules, "handoff.on_price_ask", True):
        hit = _hit(ctx["text"], _PRICE_WORDS)
        if hit:
            return "handoff", f"涉及价格/折扣/赔付（命中「{hit}」），AI 不得承诺", "handoff.on_price_ask"
    return "", "", ""


def _step_procedure(ctx: dict, rules: dict) -> tuple:
    if ctx["matched_procedure"]:
        return "procedure", f"命中剧本 {ctx['matched_procedure']}", "route.order"
    return "", "", ""


def _step_appointment(ctx: dict, rules: dict) -> tuple:
    if ctx["intent"] == "预约":
        return "appointment", "意图为预约相关", "route.order"
    return "", "", ""


def _step_action(ctx: dict, rules: dict) -> tuple:
    if ctx["intent"] in ctx["defined_actions"]:
        return "action", f"意图 {ctx['intent']} 命中已定义动作", "route.order"
    return "", "", ""


def _step_kb(ctx: dict, rules: dict) -> tuple:
    min_conf = _rule(rules, "handoff.confidence", 0.80, float)
    if ctx["intent"] == "咨询":
        if ctx["confidence"] >= min_conf:
            return "kb", f"咨询类且把握度 {ctx['confidence']:.2f} ≥ {min_conf}", "handoff.confidence"
        return "handoff", (f"咨询类但把握度 {ctx['confidence']:.2f} < {min_conf}，"
                           f"拿不准不硬答"), "handoff.confidence"
    return "", "", ""


def _step_handoff_turns(ctx: dict, rules: dict) -> tuple:
    max_turns = _rule(rules, "handoff.max_turns", 3, int)
    if ctx["unresolved_turns"] >= max_turns:
        return "handoff", (f"连续 {ctx['unresolved_turns']} 轮未解决，"
                           f"达到上限 {max_turns}"), "handoff.max_turns"
    return "", "", ""


def _step_fallback(ctx: dict, rules: dict) -> tuple:
    return "self_answer", "未命中任何分流条件，走自答", "route.order"


_STEPS = {
    "handoff_first": _step_handoff_first,
    "procedure": _step_procedure,
    "appointment": _step_appointment,
    "action": _step_action,
    "kb": _step_kb,
    "handoff_turns": _step_handoff_turns,
    "fallback": _step_fallback,
}


# --------------------------------------------------------------------------- 入口
async def execute(input_data: dict, runtime_context: dict) -> dict:
    """
    input_data:
      intent_main            必填，来自 intent_classify
      emotion / confidence   情绪与把握度
      risk_flags             客户卡风险位（来自 read_view 第②槽）
      matched_procedure      命中的剧本标识
      unresolved_turns       连续未解决轮数
      customer_asked_human   客户是否明说要人工
      text                   本轮原文，用于关键词兜底识别
      defined_actions        已定义动作清单，缺省用内置默认

    返回：path / reason / matched_rule / step / evaluated / notes；
          失败返回 {"error": "..."}，不抛异常。
    """
    rc = runtime_context or {}
    inp = input_data or {}
    notes: list = []

    intent = str(inp.get("intent_main") or "").strip()
    if not intent:
        return {"error": "缺少必填参数: intent_main"}

    rules = rc.get("rules") or {}
    actions = inp.get("defined_actions")
    ctx = {
        "intent": intent,
        "emotion": str(inp.get("emotion") or "中性").strip(),
        "confidence": _rule({"c": inp.get("confidence")}, "c", 0.0, float),
        "risk_flags": list(inp.get("risk_flags") or []),
        "matched_procedure": str(inp.get("matched_procedure") or "").strip(),
        "unresolved_turns": _rule({"t": inp.get("unresolved_turns")}, "t", 0, int),
        "asked_human": bool(inp.get("customer_asked_human", False)),
        "text": str(inp.get("text") or ""),
        "defined_actions": tuple(actions) if actions else _DEFAULT_ACTIONS,
    }

    evaluated: list = []
    for idx, name in enumerate(_order(rules, notes), start=1):
        fn = _STEPS.get(name)
        if fn is None:
            continue
        try:
            path, reason, rule_key = fn(ctx, rules)
        except Exception as e:                       # noqa: BLE001 单步隔离
            logger.exception("路由第 %s 步 %s 求值失败: %r", idx, name, e)
            notes.append(f"步骤 {name} 求值失败，已跳过: {e}")
            evaluated.append({"step": idx, "name": name, "hit": False, "error": str(e)})
            continue
        if path:
            evaluated.append({"step": idx, "name": name, "hit": True,
                              "path": path, "reason": reason})
            return {"path": path, "reason": reason, "matched_rule": rule_key,
                    "step": idx, "step_name": name, "evaluated": evaluated,
                    "risk_flags": ctx["risk_flags"], "notes": notes}
        evaluated.append({"step": idx, "name": name, "hit": False})

    # 顺序被改到连 fallback 都没有时的兜底：任何输入都必须有出路
    notes.append("判定顺序未包含 fallback，按 self_answer 兜底")
    return {"path": "self_answer", "reason": "所有步骤未命中，兜底自答",
            "matched_rule": "route.order", "step": 0, "step_name": "fallback",
            "evaluated": evaluated, "risk_flags": ctx["risk_flags"], "notes": notes}
