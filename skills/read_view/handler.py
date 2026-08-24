"""
skills/read_view/handler.py

S-03 取客户资料：一次拉齐客户卡九个槽位，按字段定义做脱敏和 AI 可见性过滤。

设计要点：
  ① 只读零 token —— 不写任何表，不调模型，天然幂等。
  ② 单槽失败不整体失败 —— 九槽各自 try/except + rollback，
     客户卡少一块比打不开强（施工规范第 9 条）。
  ③ 风险条永远第二位且不截断 —— 坐席三秒内只看最上两块，
     被 char_cap 砍掉的风险提示等于没有。
  ④ for_ai=true 时敏感字段只出掩码 —— view.expose_sensitive 是极高风险规则，
     默认关；开启后敏感信息会进模型上下文与日志，泄露不可逆。
  ⑤ 顺序取而非并发 —— 平台注入单个 AsyncSession，并发复用会抛
     InvalidRequestError。对外语义（超时留空 / partial=true）与手册一致；
     若平台改注入 session factory，只需改 _gather_sections 一处。

runtime_context：{org_id, role_id, staff_id, conv_id, rules, db, models}

依赖：pip install sqlalchemy[asyncio] asyncpg
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger("myinc.skills.read_view")

_REQUIRED_TABLES = ("crm_customers",)

# 九槽固定顺序。风险条钉死在第二位——它决定这句话能不能说。
_SECTIONS = ("profile_line", "risk_bar", "member_card", "recent_orders",
             "preferences", "open_loops", "next_appointment", "relations", "metrics")

# 风险类标签：永远压制记忆和画像。人工说了勿扰，AI 算出他是高潜客户也不能发。
_RISK_TAGS = ("勿扰", "法务关注", "有纠纷", "特批价", "投诉")

# 无论 field_defs 怎么配，这几类字段永不进 AI 上下文
_NEVER_TO_AI = ("cost", "底价", "成本", "internal_note", "内部备注", "margin", "毛利")

_SLOT_TIMEOUT_S = 0.2      # 手册：单槽 200ms 超时留空并标 partial
_MASK_KEEP_HEAD = 3
_MASK_KEEP_TAIL = 4


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


def _on(rules: dict, key: str, default: bool) -> bool:
    raw = _rule(rules, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("开", "true", "1", "yes", "on", "启用")


def _load_json(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _fmt(val) -> str:
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return str(val)


# --------------------------------------------------------------------------- 脱敏与可见性
def _mask(value: str) -> str:
    """138****0000：保留头尾便于坐席核对，中间打码。"""
    s = str(value or "")
    if len(s) <= _MASK_KEEP_HEAD + _MASK_KEEP_TAIL:
        return "*" * len(s)
    return f"{s[:_MASK_KEEP_HEAD]}{'*' * (len(s) - _MASK_KEEP_HEAD - _MASK_KEEP_TAIL)}{s[-_MASK_KEEP_TAIL:]}"


async def _field_policy(session, schema: str, tables: set) -> dict:
    """
    从 crm_field_defs 读字段可见性策略。表不存在时退回保守默认：
    phone/email 视作敏感，其余可见——宁可多打码，不可少打码。
    """
    fallback = {"crm_customers:phone": {"sensitive": True, "to_ai": False},
                "crm_customers:email": {"sensitive": True, "to_ai": False}}
    if "crm_field_defs" not in tables:
        return fallback
    tbl = _tbl(schema, "crm_field_defs")
    cols = ", ".join(_qi(c) for c in
                     ("def_key", "table_name", "field_name", "is_sensitive",
                      "expose_to_ai", "is_visible", "label_cn"))
    sql = f"SELECT {cols} FROM {tbl} WHERE {_qi('state')} = :state"
    rows = (await _exec(session, sql, {"state": "启用"})).fetchall()
    policy = dict(fallback)
    for r in rows:
        key = r[0] or f"{r[1]}:{r[2]}"
        policy[key] = {"sensitive": bool(r[3]), "to_ai": bool(r[4]),
                       "visible": bool(r[5]), "label": r[6]}
    return policy


def _apply_policy(table: str, field: str, value, policy: dict, for_ai: bool,
                  expose_sensitive: bool, masked: list, hidden: list):
    """
    返回 (是否保留, 处理后的值)。
    for_ai=true 且 expose_to_ai=false → 整个不放进结果（不是掩码，是消失）。
    """
    if for_ai and any(k in field.lower() for k in _NEVER_TO_AI):
        hidden.append(f"{table}.{field}")
        return False, None
    pol = policy.get(f"{table}:{field}") or {}
    if for_ai and pol.get("to_ai") is False:
        hidden.append(f"{table}.{field}")
        return False, None
    if pol.get("sensitive"):
        if for_ai and not expose_sensitive:
            masked.append(f"{table}.{field}")
            return True, _mask(value)
        if not for_ai:
            masked.append(f"{table}.{field}")
            return True, _mask(value)
    return True, value


def _cap(items: list, cap: int, exempt: bool) -> tuple:
    """按字数上限截断槽位内容。风险条豁免——被截断的风险提示等于没有。"""
    if exempt or cap <= 0:
        return items, False
    out, used, cut = [], 0, False
    for it in items:
        s = it if isinstance(it, str) else json.dumps(it, ensure_ascii=False)
        if used + len(s) > cap:
            cut = True
            break
        out.append(it)
        used += len(s)
    return out, cut


# --------------------------------------------------------------------------- 九槽取数
async def _slot_customer(session, schema: str, cust_id: str) -> dict | None:
    tbl = _tbl(schema, "crm_customers")
    fields = ("cust_id", "name", "cust_type", "status", "company", "title",
              "phone", "email", "region", "industry", "do_not_touch",
              "owner_staff", "note", "custom_fields", "first_seen_at", "last_seen_at")
    cols = ", ".join(_qi(c) for c in fields)
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE LIMIT 1")
    row = (await _exec(session, sql, {"cid": cust_id})).fetchone()
    return dict(zip(fields, row)) if row else None


async def _slot_risk(session, schema: str, tables: set, cust_id: str,
                     customer: dict) -> list:
    """风险条：风险标签 + do_not_touch + 钉选记忆。标签永远压制记忆和画像。"""
    items = []
    if customer.get("do_not_touch"):
        items.append({"level": "高", "text": "勿扰客户", "source": "crm_customers.do_not_touch"})
    if "crm_customer_tags" in tables:
        tbl = _tbl(schema, "crm_customer_tags")
        cols = ", ".join(_qi(c) for c in ("tag_name", "tag_value", "reason", "set_by", "source"))
        sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
               f"AND {_qi('state')} = :st "
               f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE")
        for r in (await _exec(session, sql, {"cid": cust_id, "st": "生效"})).fetchall():
            if r[0] in _RISK_TAGS:
                items.append({"level": "高", "text": f"{r[0]}{('：' + r[1]) if r[1] else ''}",
                              "reason": r[2], "set_by": r[3],
                              "source": f"crm_customer_tags（{r[4]}）"})
    if "crm_memories" in tables:
        tbl = _tbl(schema, "crm_memories")
        cols = ", ".join(_qi(c) for c in ("content", "evidence", "mem_type"))
        sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
               f"AND {_qi('mem_type')} = :mt AND {_qi('state')} = :st "
               f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE")
        for r in (await _exec(session, sql, {"cid": cust_id, "mt": "钉选", "st": "生效"})).fetchall():
            items.append({"level": "高", "text": r[0], "evidence": r[1],
                          "source": "crm_memories（钉选，永不被 AI 覆盖）"})
    return items


async def _slot_memories(session, schema: str, cust_id: str, top_n: int) -> list:
    """偏好与禁忌：hit_count×confidence 降序，钉选置顶（view.memories_rank）。"""
    tbl = _tbl(schema, "crm_memories")
    cols = ", ".join(_qi(c) for c in
                     ("mem_type", "content", "evidence", "confidence",
                      "hit_count", "source_conv_id"))
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
           f"AND {_qi('state')} = :st "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE")
    rows = (await _exec(session, sql, {"cid": cust_id, "st": "生效"})).fetchall()
    scored = []
    for r in rows:
        pinned = r[0] == "钉选"
        score = float(r[3] or 0) * max(int(r[4] or 0), 1)
        scored.append((0 if pinned else 1, -score,
                       {"type": r[0], "text": r[1], "evidence": r[2],
                        "confidence": float(r[3] or 0), "hit_count": int(r[4] or 0),
                        "source": f"crm_memories / 会话 {r[5] or '—'}"}))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [s[2] for s in scored[:top_n]]


async def _slot_orders(session, schema: str, cust_id: str, n: int) -> list:
    tbl = _tbl(schema, "crm_mock_orders")
    cols = ", ".join(_qi(c) for c in ("order_id", "product_name", "amount",
                                      "order_state", "created_at"))
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
           f"ORDER BY {_qi('created_at')} DESC NULLS LAST LIMIT {int(n)}")
    return [{"order_id": r[0], "product": r[1], "amount": r[2],
             "state": r[3], "at": _fmt(r[4]), "source": "crm_mock_orders"}
            for r in (await _exec(session, sql, {"cid": cust_id})).fetchall()]


async def _slot_member(session, schema: str, cust_id: str) -> list:
    tbl = _tbl(schema, "crm_members")
    cols = ", ".join(_qi(c) for c in ("level", "points", "state", "valid_to"))
    sql = f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid LIMIT 1"
    r = (await _exec(session, sql, {"cid": cust_id})).fetchone()
    if not r:
        return []
    return [{"level": r[0], "points": r[1], "state": r[2],
             "valid_to": _fmt(r[3]), "source": "crm_members"}]


async def _slot_open_loops(session, schema: str, tables: set, cust_id: str) -> list:
    """未闭环：没解决的会话 + 未关闭的工单。"""
    items = []
    if "crm_conversations" in tables:
        tbl = _tbl(schema, "crm_conversations")
        cols = ", ".join(_qi(c) for c in ("conv_id", "intent_main", "summary", "started_at"))
        sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
               f"AND COALESCE({_qi('goal_met')}, FALSE) = FALSE "
               f"AND {_qi('state')} = :st "
               f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE "
               f"ORDER BY {_qi('started_at')} DESC NULLS LAST LIMIT 5")
        for r in (await _exec(session, sql, {"cid": cust_id, "st": "已结束"})).fetchall():
            items.append({"kind": "未解决会话", "conv_id": r[0], "intent": r[1],
                          "summary": r[2], "at": _fmt(r[3]), "source": "crm_conversations"})
    if "crm_tickets" in tables:
        tbl = _tbl(schema, "crm_tickets")
        cols = ", ".join(_qi(c) for c in ("ticket_id", "title", "state", "created_at"))
        sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
               f"AND {_qi('state')} <> :closed "
               f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE LIMIT 5")
        for r in (await _exec(session, sql, {"cid": cust_id, "closed": "已关闭"})).fetchall():
            items.append({"kind": "未关闭工单", "ticket_id": r[0], "title": r[1],
                          "state": r[2], "at": _fmt(r[3]), "source": "crm_tickets"})
    return items


async def _slot_appointment(session, schema: str, cust_id: str) -> list:
    tbl = _tbl(schema, "crm_appointments")
    cols = ", ".join(_qi(c) for c in ("appt_id", "service_name", "start_at", "state"))
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
           f"AND {_qi('start_at')} >= :now "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE "
           f"ORDER BY {_qi('start_at')} ASC LIMIT 1")
    now = datetime.now(timezone.utc)
    r = (await _exec(session, sql, {"cid": cust_id, "now": now})).fetchone()
    if not r:
        return []
    return [{"appt_id": r[0], "service": r[1], "start_at": _fmt(r[2]),
             "state": r[3], "source": "crm_appointments"}]


async def _slot_relations(session, schema: str, cust_id: str) -> list:
    tbl = _tbl(schema, "crm_relations")
    cols = ", ".join(_qi(c) for c in ("rel_type", "to_name", "to_cust_id",
                                      "rel_role", "state"))
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('from_cust_id')} = :cid "
           f"AND {_qi('state')} = :st LIMIT 10")
    return [{"rel_type": r[0], "to_name": r[1], "to_cust_id": r[2],
             "role": r[3], "source": "crm_relations"}
            for r in (await _exec(session, sql, {"cid": cust_id, "st": "生效"})).fetchall()]


def _slot_metrics(customer: dict) -> list:
    """指标由 metrics_compute 写在 customers.custom_fields 里，本技能只读不算。"""
    custom = _load_json(customer.get("custom_fields"))
    metrics = custom.get("metrics") or {}
    if not isinstance(metrics, dict) or not metrics:
        return []
    return [{"name": k, "value": v, "source": "crm_customers.custom_fields.metrics"}
            for k, v in metrics.items()]


def _slot_profile_line(customer: dict, profile_row) -> list:
    if profile_row and profile_row[0]:
        return [{"text": profile_row[0], "version": profile_row[1],
                 "source": "crm_profiles"}]
    bits = [customer.get("name"), customer.get("company"), customer.get("title"),
            customer.get("region"), customer.get("industry")]
    line = " · ".join(b for b in bits if b)
    return [{"text": line or "（暂无画像）", "source": "crm_customers（画像未生成，按主档拼）"}]


# --------------------------------------------------------------------------- 编排
async def _gather_sections(session, schema: str, tables: set, cust_id: str,
                           customer: dict, wanted: tuple, rules: dict,
                           failed: list, skipped: list) -> dict:
    """
    顺序取九槽，每槽独立超时 + try/except + rollback。
    一槽失败不连坐其余八槽——客户卡少一块比打不开强。
    """
    top_n = _rule(rules, "view.memories_top_n", 8, int)
    orders_n = _rule(rules, "view.orders_recent_n", 3, int)

    async def _profile():
        if "crm_profiles" not in tables:
            skipped.append({"section": "profile_line", "reason": "crm_profiles 表不存在"})
            return _slot_profile_line(customer, None)
        tbl = _tbl(schema, "crm_profiles")
        cols = ", ".join(_qi(c) for c in ("one_liner", "version"))
        sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
               f"ORDER BY {_qi('version')} DESC NULLS LAST LIMIT 1")
        return _slot_profile_line(
            customer, (await _exec(session, sql, {"cid": cust_id})).fetchone())

    def _need(name: str, table: str):
        if table not in tables:
            skipped.append({"section": name, "reason": f"{table} 表不存在"})
            return False
        return True

    builders = {
        "profile_line": _profile,
        "risk_bar": lambda: _slot_risk(session, schema, tables, cust_id, customer),
        "member_card": (lambda: _slot_member(session, schema, cust_id))
                       if _need("member_card", "crm_members") else None,
        "recent_orders": (lambda: _slot_orders(session, schema, cust_id, orders_n))
                         if _need("recent_orders", "crm_mock_orders") else None,
        "preferences": (lambda: _slot_memories(session, schema, cust_id, top_n))
                       if _need("preferences", "crm_memories") else None,
        "open_loops": lambda: _slot_open_loops(session, schema, tables, cust_id),
        "next_appointment": (lambda: _slot_appointment(session, schema, cust_id))
                            if _need("next_appointment", "crm_appointments") else None,
        "relations": (lambda: _slot_relations(session, schema, cust_id))
                     if _need("relations", "crm_relations") else None,
    }

    out: dict = {}
    for name in wanted:
        if name == "metrics":
            out[name] = {"items": _slot_metrics(customer), "truncated": False}
            continue
        builder = builders.get(name)
        if builder is None:
            out[name] = {"items": [], "truncated": False}
            continue
        try:
            out[name] = {"items": await asyncio.wait_for(builder(), _SLOT_TIMEOUT_S),
                         "truncated": False}
        except asyncio.TimeoutError:
            logger.warning("槽位 %s 超过 %sms 未返回", name, int(_SLOT_TIMEOUT_S * 1000))
            failed.append({"section": name, "reason": f"超时 >{int(_SLOT_TIMEOUT_S * 1000)}ms"})
            out[name] = {"items": [], "truncated": False}
        except Exception as e:                       # noqa: BLE001 单槽隔离
            logger.exception("槽位 %s 取数失败: %r", name, e)
            failed.append({"section": name, "reason": str(e)})
            out[name] = {"items": [], "truncated": False}
            try:
                await session.rollback()             # 脏事务不能连累后面的槽
            except Exception as re_:                 # noqa: BLE001
                logger.exception("rollback 失败: %r", re_)
    return out


# --------------------------------------------------------------------------- 入口
async def execute(input_data: dict, runtime_context: dict) -> dict:
    """
    input_data:
      cust_id   客户编号，必填
      sections  只取指定槽位，缺省九槽全取
      for_ai    默认 False。True 时执行 AI 可见性过滤与敏感字段掩码

    返回：sections / partial / failed_sections / masked_fields / hidden_fields /
          elapsed_ms / skipped / notes；失败返回 {"error": "..."}，不抛异常。
    """
    started = time.monotonic()
    rc = runtime_context or {}
    inp = input_data or {}
    notes: list = []
    skipped: list = []
    failed: list = []
    masked: list = []
    hidden: list = []

    cust_id = str(inp.get("cust_id") or "").strip()
    if not cust_id:
        return {"error": "缺少必填参数: cust_id"}

    session = _get_session(rc)
    if session is None:
        return {"error": "runtime_context 缺少 db（SQLAlchemy AsyncSession）"}
    schema = _org_schema(rc)
    if not schema:
        return {"error": "runtime_context 缺少 org_id，拒绝猜 schema"}

    rules = rc.get("rules") or {}
    for_ai = bool(inp.get("for_ai", False))
    expose_sensitive = _on(rules, "view.expose_sensitive", False)
    cap = _rule(rules, "view.section_char_cap", 300, int)

    wanted = tuple(inp.get("sections") or _SECTIONS)
    unknown = [s for s in wanted if s not in _SECTIONS]
    if unknown:
        return {"error": f"未知槽位: {', '.join(unknown)}", "supported": list(_SECTIONS)}

    try:
        tables = await _existing_tables(session, schema)
        lost = [t for t in _REQUIRED_TABLES if t not in tables]
        if lost:
            return {"error": f"必需表缺失: {', '.join(lost)}（schema={schema}）"}

        customer = await _slot_customer(session, schema, cust_id)
        if not customer:
            return {"error": f"客户不存在或已软删: {cust_id}"}

        policy = await _field_policy(session, schema, tables)
        for field in ("phone", "email", "note"):
            keep, val = _apply_policy("crm_customers", field, customer.get(field),
                                      policy, for_ai, expose_sensitive, masked, hidden)
            customer[field] = val if keep else None

        sections = await _gather_sections(session, schema, tables, cust_id,
                                          customer, wanted, rules, failed, skipped)

        for name, slot in sections.items():
            items, cut = _cap(slot["items"], cap, exempt=(name == "risk_bar"))
            slot["items"], slot["truncated"] = items, cut
            slot["count"] = len(items)

        if expose_sensitive and for_ai:
            notes.append("view.expose_sensitive 已开启（极高风险）：敏感字段原值已进入 AI 上下文")

        return {"cust_id": cust_id, "for_ai": for_ai,
                "sections": {k: sections[k] for k in wanted if k in sections},
                "section_order": list(wanted),
                "partial": bool(failed), "failed_sections": failed,
                "masked_fields": sorted(set(masked)),
                "hidden_fields": sorted(set(hidden)),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "skipped": skipped, "notes": notes}

    except Exception as e:                           # noqa: BLE001 入口兜底
        logger.exception("read_view 失败: %r", e)
        try:
            await session.rollback()
        except Exception as re_:                     # noqa: BLE001
            logger.exception("rollback 失败: %r", re_)
        return {"error": str(e), "failed_sections": failed,
                "skipped": skipped, "notes": notes}
