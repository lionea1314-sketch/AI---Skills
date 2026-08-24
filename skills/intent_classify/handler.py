"""
skills/intent_classify/handler.py

S-04 认意图和情绪：读最后一轮客户消息，结合上下文，输出意图与情绪分类。

设计要点：
  ① 只分类不回答 —— 提示词把模型限制在输出 JSON 上，任何自由发挥都会被
     JSON 解析拒掉并走降级路径。
  ② 判错的代价是路由错 —— 低于 intent.min_confidence 一律强制归"其他"，
     宁可多走人工兜底，也不让低把握结果驱动自动化动作。
  ③ 模型不可用不能断链 —— 缺模型/超时/返回非 JSON 时退回关键词规则，
     degraded=true 且 confidence 封顶 0.45（必然落进"其他"）。
  ④ 落库是可选副作用 —— 没有 conv_id 或没有 crm_conversations 表时
     只返回不写库，不报错（施工规范第 6 条：选配缺失是跳过）。

runtime_context：{org_id, role_id, staff_id, conv_id, rules, db, models}

依赖：pip install sqlalchemy[asyncio] asyncpg
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from sqlalchemy import text

logger = logging.getLogger("myinc.skills.intent_classify")

_DEFAULT_CLASSES = ("咨询", "售后", "投诉", "购买", "会员", "预约", "其他")
_EMOTIONS = ("正向", "中性", "负向")

_MODEL_TIMEOUT_S = 8.0
_DEGRADED_CONFIDENCE_CAP = 0.45     # 低于 min_confidence 默认值 0.5，必然落"其他"

_PROMPT_DEFAULT = """你只做分类，不回答问题。读最后一轮客户消息，结合上下文，输出 JSON：
{{"intent_main":"", "sub_intent":"", "emotion":"", "confidence":0.0}}
intent_main 只能从清单里选（清单可在界面增删）：{class_list}
emotion 只能是：正向/中性/负向
准则：
· 只看客户说的，不看我方说的
· 一句话里有多个意图，选最需要立刻处理的那个
· 反问句、语气词、标点密度参与情绪判断，但不要过度解读
· 不确定就给低 confidence 并选"其他"，不要硬选

上下文：
{context}

本轮客户消息：
{text}"""

# 降级用的关键词表。只做粗分，够把消息送进正确的兜底路径即可。
_KEYWORDS = {
    "投诉": ("投诉", "曝光", "315", "消协", "起诉", "差评", "举报"),
    "售后": ("退货", "退款", "换货", "维修", "坏了", "破损", "漏发", "少发", "物流", "快递"),
    "购买": ("怎么买", "下单", "多少钱", "有货", "现货", "优惠", "折扣", "开票"),
    "会员": ("积分", "会员", "等级", "兑换", "权益"),
    "预约": ("预约", "改期", "取消预约", "什么时候有空", "约个"),
    "咨询": ("请问", "咨询", "怎么用", "参数", "尺寸", "型号", "能不能"),
}
_NEG_WORDS = ("垃圾", "骗", "太差", "无语", "气死", "投诉", "退钱", "过分",
              "什么破", "恶心", "坑人", "毛病")
_POS_WORDS = ("谢谢", "满意", "不错", "好评", "太棒", "感谢", "喜欢")


# --------------------------------------------------------------------------- 会话/表定位
def _get_session(rc: dict):
    for key in ("db", "session", "db_session"):
        sess = (rc or {}).get(key)
        if sess is not None:
            return sess
    return None


def _org_schema(rc: dict) -> str:
    org_id = str((rc or {}).get("org_id") or "").replace("-", "").strip()
    return f"org_data_{org_id}" if org_id else ""


def _qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _tbl(schema: str, table: str) -> str:
    return f"{schema}.{_qi(table)}"


_UNSAFE_CAST = re.compile(r":\w+::")


def _assert_safe_sql(sql: str) -> None:
    if _UNSAFE_CAST.search(sql):
        raise ValueError(f"SQL 含 :name::type 写法，asyncpg 会解析失败: {sql[:120]}")


async def _exec(session, sql: str, params: dict | None = None):
    _assert_safe_sql(sql)
    return await session.execute(text(sql), params or {})


async def _existing_tables(session, schema: str) -> set:
    sql = ("SELECT table_name FROM information_schema.tables "
           "WHERE table_schema = :schema")
    rows = await _exec(session, sql, {"schema": schema})
    return {r[0] for r in rows.fetchall()}


def _rule(rules: dict, key: str, default, cast=None):
    raw = (rules or {}).get(key, default)
    if cast is None:
        return raw
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("规则 %s 值非法(%r)，退回默认 %r", key, raw, default)
        return default


def _class_list(rules: dict) -> tuple:
    raw = _rule(rules, "intent.class_list", None)
    if not raw:
        return _DEFAULT_CLASSES
    if isinstance(raw, str):
        raw = [x.strip() for x in re.split(r"[,，/\s]+", raw) if x.strip()]
    items = tuple(raw)
    return items if "其他" in items else items + ("其他",)


# --------------------------------------------------------------------------- 模型调用
async def _call_model(models, prompt: str, notes: list) -> tuple:
    """
    逐个尝试平台可能提供的模型接口形态。
    任何一种成功即返回 (原始文本, 模型标识)；全都不行返回 ("", None) 走降级。
    """
    if models is None:
        notes.append("runtime_context 未提供 models，走降级规则")
        return "", None

    attempts = (
        ("complete", lambda: models.complete(prompt=prompt, tier="fast")),
        ("chat", lambda: models.chat(messages=[{"role": "user", "content": prompt}],
                                     tier="fast")),
        ("callable", lambda: models(prompt)),
    )
    for name, call in attempts:
        if name != "callable" and not hasattr(models, name):
            continue
        if name == "callable" and not callable(models):
            continue
        try:
            res = call()
            if asyncio.iscoroutine(res):
                res = await asyncio.wait_for(res, _MODEL_TIMEOUT_S)
            if isinstance(res, str):
                return res, name
            for key in ("text", "content", "output", "message"):
                val = (res or {}).get(key) if isinstance(res, dict) else getattr(res, key, None)
                if isinstance(val, str) and val.strip():
                    return val, name
            notes.append(f"models.{name} 返回结构无法识别: {str(res)[:120]}")
        except asyncio.TimeoutError:
            logger.warning("模型调用超时 (%ss)", _MODEL_TIMEOUT_S)
            notes.append(f"models.{name} 超时 {_MODEL_TIMEOUT_S}s")
        except Exception as e:                       # noqa: BLE001 逐形态兜底
            logger.exception("models.%s 调用失败: %r", name, e)
            notes.append(f"models.{name} 调用失败: {e}")
    return "", None


def _parse_json(raw: str) -> dict | None:
    """模型可能裹着 ```json 或前后多说两句，抠出第一个 JSON 对象。"""
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        val = json.loads(m.group(0))
        return val if isinstance(val, dict) else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 降级规则
def _degrade(body: str, sensitivity: str) -> dict:
    """模型不可用时的关键词粗分。只求把消息送进正确的兜底路径。"""
    intent, hit = "其他", None
    for name, words in _KEYWORDS.items():
        for w in words:
            if w in body:
                intent, hit = name, w
                break
        if hit:
            break

    neg = sum(1 for w in _NEG_WORDS if w in body)
    pos = sum(1 for w in _POS_WORDS if w in body)
    marks = body.count("!") + body.count("！") + body.count("?") + body.count("？")
    threshold = {"高": 0, "中": 1, "低": 2}.get(str(sensitivity), 1)
    if neg > pos and (neg + (1 if marks >= 3 else 0)) > threshold:
        emotion = "负向"
    elif pos > neg:
        emotion = "正向"
    else:
        emotion = "中性"

    return {"intent_main": intent, "sub_intent": hit or "", "emotion": emotion,
            "confidence": _DEGRADED_CONFIDENCE_CAP if hit else 0.2}


# --------------------------------------------------------------------------- 上下文与写回
async def _load_context(session, schema: str, conv_id: str, turns: int) -> list:
    """
    正文在平台消息流水里，会话表只存档案。这里取会话级摘要作为上下文兜底——
    平台若在 input_data.context 里直接给了原文，优先用那个。
    """
    if turns <= 0:
        return []
    tbl = _tbl(schema, "crm_conversations")
    cols = ", ".join(_qi(c) for c in ("summary", "intent_main", "turn_count"))
    sql = f"SELECT {cols} FROM {tbl} WHERE {_qi('conv_id')} = :cid LIMIT 1"
    row = (await _exec(session, sql, {"cid": conv_id})).fetchone()
    if not row:
        return []
    out = []
    if row[0]:
        out.append({"role": "摘要", "text": str(row[0])})
    if row[1]:
        out.append({"role": "上轮意图", "text": str(row[1])})
    return out[:turns]


async def _write_back(session, schema: str, conv_id: str,
                      intent: str, emotion: str) -> int:
    tbl = _tbl(schema, "crm_conversations")
    sql = (f"UPDATE {tbl} SET {_qi('intent_main')} = :intent, {_qi('emotion')} = :emotion "
           f"WHERE {_qi('conv_id')} = :cid")
    res = await _exec(session, sql, {"intent": intent, "emotion": emotion, "cid": conv_id})
    await session.commit()          # AsyncSession 不自动提交，漏了会静默回滚
    return int(res.rowcount or 0)


# --------------------------------------------------------------------------- 入口
async def execute(input_data: dict, runtime_context: dict) -> dict:
    """
    input_data:
      text     本轮客户消息，必填
      conv_id  写回分类结果用；缺省只返回不落库
      context  前 N 轮 [{role, text}]，缺省按 intent.context_turns 从库取
      cust_id  仅用于日志归因

    返回：intent_main / sub_intent / emotion / confidence / low_confidence /
          degraded / model_used / written / skipped / notes；
          失败返回 {"error": "..."}，不抛异常。
    """
    rc = runtime_context or {}
    inp = input_data or {}
    notes: list = []
    skipped: list = []

    body = str(inp.get("text") or "").strip()
    if not body:
        return {"error": "缺少必填参数: text"}

    rules = rc.get("rules") or {}
    classes = _class_list(rules)
    turns = _rule(rules, "intent.context_turns", 3, int)
    min_conf = _rule(rules, "intent.min_confidence", 0.5, float)
    sensitivity = _rule(rules, "intent.emotion_negative_sensitivity", "中")

    conv_id = str(inp.get("conv_id") or rc.get("conv_id") or "").strip()
    session = _get_session(rc)
    schema = _org_schema(rc)
    tables: set = set()

    # 1) 上下文：入参优先，其次查库；查库失败不阻断分类
    context = inp.get("context") or []
    if not context and conv_id and session is not None and schema:
        try:
            tables = await _existing_tables(session, schema)
            if "crm_conversations" in tables:
                context = await _load_context(session, schema, conv_id, turns)
            else:
                skipped.append({"table": "crm_conversations", "reason": "表不存在，跳过取上下文"})
        except Exception as e:                       # noqa: BLE001 上下文是可选输入
            logger.exception("取上下文失败: %r", e)
            notes.append(f"取上下文失败: {e}")
            try:
                await session.rollback()
            except Exception as re_:                 # noqa: BLE001
                logger.exception("rollback 失败: %r", re_)

    ctx_text = "\n".join(
        f"{c.get('role', '')}: {c.get('text', '')}" for c in context[:turns]) or "（无）"

    # 2) 调模型；失败走降级
    template = _rule(rules, "intent.prompt_template", None) or _PROMPT_DEFAULT
    try:
        prompt = template.format(class_list="/".join(classes), context=ctx_text, text=body)
    except (KeyError, IndexError) as e:
        logger.exception("提示词模板占位符错误: %r", e)
        notes.append(f"intent.prompt_template 占位符错误({e})，退回内置默认")
        prompt = _PROMPT_DEFAULT.format(
            class_list="/".join(classes), context=ctx_text, text=body)

    raw, model_used = await _call_model(rc.get("models"), prompt, notes)
    parsed = _parse_json(raw)
    degraded = parsed is None
    if degraded:
        if raw:
            notes.append(f"模型返回非 JSON，已降级。原始返回前 200 字: {raw[:200]}")
        result = _degrade(body, sensitivity)
        model_used = None
    else:
        result = {
            "intent_main": str(parsed.get("intent_main") or "").strip(),
            "sub_intent": str(parsed.get("sub_intent") or "").strip(),
            "emotion": str(parsed.get("emotion") or "").strip(),
            "confidence": _rule({"c": parsed.get("confidence")}, "c", 0.0, float),
        }

    # 3) 值域收敛：模型可能给出清单外的值，一律按"其他/中性"处理
    if result["intent_main"] not in classes:
        if not degraded:
            notes.append(f"模型给出清单外意图 {result['intent_main']!r}，已归为「其他」")
        result["intent_main"] = "其他"
    if result["emotion"] not in _EMOTIONS:
        result["emotion"] = "中性"
    result["confidence"] = max(0.0, min(1.0, float(result["confidence"] or 0)))
    if degraded:
        result["confidence"] = min(result["confidence"], _DEGRADED_CONFIDENCE_CAP)

    # 4) 门槛：低把握一律归"其他"，宁可走人工兜底
    low = result["confidence"] < min_conf
    if low and result["intent_main"] != "其他":
        notes.append(f"confidence {result['confidence']:.2f} < {min_conf}，强制归「其他」")
        result["intent_main"] = "其他"

    # 5) 写回：可选副作用，失败不影响分类结果返回
    written = 0
    if conv_id and session is not None and schema:
        try:
            if not tables:
                tables = await _existing_tables(session, schema)
            if "crm_conversations" in tables:
                written = await _write_back(session, schema, conv_id,
                                            result["intent_main"], result["emotion"])
            else:
                skipped.append({"table": "crm_conversations", "reason": "表不存在，跳过写回"})
        except Exception as e:                       # noqa: BLE001 写回是副作用
            logger.exception("写回会话分类失败: %r", e)
            notes.append(f"写回失败: {e}")
            try:
                await session.rollback()
            except Exception as re_:                 # noqa: BLE001
                logger.exception("rollback 失败: %r", re_)
    elif conv_id:
        skipped.append({"write_back": conv_id, "reason": "缺 db 或 org_id，只返回不落库"})

    return {**result, "low_confidence": low, "degraded": degraded,
            "model_used": model_used, "class_list": list(classes),
            "context_turns_used": len(context[:turns]), "written": written,
            "skipped": skipped, "notes": notes}
