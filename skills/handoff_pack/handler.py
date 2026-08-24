"""
skills/handoff_pack/handler.py

S-06 转人工：选出接手人，生成交接包六件套，改写会话归属并留痕。

设计要点：
  ① 客户侧零动作 —— 对客户永远只有一个身份，客户端不出现"转接"字样，
     本技能不发送任何对客消息。禁用词表返回给 guardrail_check 在出站口执行。
  ② handled_by 是追加不是覆盖 —— 结果形如 AI,S001,S007，
     接手过的人一个都不能丢，它是"谁碰过这个客户"的唯一记录。
  ③ 没人可分派也要出交接包 —— 交接包生成不了比没人接更糟，
     至少让会话进入待认领状态，notes 说明原因。
  ④ 台账失败不回滚会话归属 —— activities 是附属数据，
     无权炸掉主数据（施工规范第 9 条）。

runtime_context：{org_id, role_id, staff_id, conv_id, rules, db, models}

依赖：pip install sqlalchemy[asyncio] asyncpg
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

logger = logging.getLogger("myinc.skills.handoff_pack")

_REQUIRED_TABLES = ("crm_conversations", "crm_staff")
_OPTIONAL_TABLES = ("crm_activities", "crm_customers", "crm_memories",
                    "crm_customer_tags")

_PACKET_ITEMS = ("customer_view", "conv_summary", "ai_attempts",
                 "next_steps", "history", "risk_bar")

_DEFAULT_TYPE_MAP = {
    "咨询": "售前组", "购买": "售前组",
    "售后": "售后组", "投诉": "售后组",
    "会员": "会员组", "预约": "前台",
}
_DEFAULT_NO_TRANSFER_WORDS = ("转接", "换人", "售后组", "转给", "工单",
                              "升级处理", "帮您转", "另一位同事")
_RISK_TAGS = ("勿扰", "法务关注", "有纠纷", "特批价", "投诉")


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


def _as_list(raw, default: tuple) -> tuple:
    if not raw:
        return default
    if isinstance(raw, str):
        return tuple(x.strip() for x in re.split(r"[,，/\s]+", raw) if x.strip())
    return tuple(raw)


def _type_map(rules: dict) -> dict:
    raw = _rule(rules, "handoff.type_map", None)
    if isinstance(raw, dict) and raw:
        return raw
    if isinstance(raw, str) and raw.strip():
        out = {}
        for pair in re.split(r"[;；]+", raw):
            m = re.split(r"[→>:：]+", pair)
            if len(m) == 2 and m[0].strip():
                out[m[0].strip()] = m[1].strip()
        if out:
            return out
    return dict(_DEFAULT_TYPE_MAP)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- 读
async def _load_conv(session, schema: str, conv_id: str) -> dict | None:
    tbl = _tbl(schema, "crm_conversations")
    fields = ("conv_id", "cust_id", "channel", "state", "intent_main", "emotion",
              "handled_by", "current_assignee", "turn_count", "summary", "started_at")
    cols = ", ".join(_qi(c) for c in fields)
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('conv_id')} = :cid "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE LIMIT 1")
    row = (await _exec(session, sql, {"cid": conv_id})).fetchone()
    return dict(zip(fields, row)) if row else None


async def _pick_staff(session, schema: str, rules: dict, intent: str,
                      owner_staff: str, notes: list) -> tuple:
    """
    选人：在职 且 当前会话数 < max_concurrent。
    负载现算——crm_conversations 里数当前指派给他的未结束会话，
    不依赖 staff 表上的计数字段（那种字段必然会和现实漂移）。
    """
    mode = str(_rule(rules, "handoff.assign_mode", "按问题类型")).strip()
    tbl_staff = _tbl(schema, "crm_staff")
    cols = ", ".join(_qi(c) for c in ("staff_id", "name", "dept", "role", "max_concurrent"))
    sql = (f"SELECT {cols} FROM {tbl_staff} WHERE {_qi('state')} = :st "
           f"AND {_qi('role')} <> :owner_role")
    rows = (await _exec(session, sql, {"st": "在职", "owner_role": "owner"})).fetchall()
    if not rows:
        return None, mode, 0

    target_dept = _type_map(rules).get(intent, "") if mode == "按问题类型" else ""
    pool = [{"staff_id": r[0], "name": r[1], "dept": r[2], "role": r[3],
             "max_concurrent": int(r[4] or 5)} for r in rows]
    if target_dept:
        matched = [s for s in pool if s["dept"] == target_dept]
        if matched:
            pool = matched
        else:
            notes.append(f"type_map 指向「{target_dept}」但无在职人员，已放宽到全员")

    # 现算负载
    tbl_conv = _tbl(schema, "crm_conversations")
    sql_load = (f"SELECT {_qi('current_assignee')}, COUNT(*) FROM {tbl_conv} "
                f"WHERE {_qi('state')} <> :closed "
                f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE "
                f"GROUP BY {_qi('current_assignee')}")
    load = {r[0]: int(r[1] or 0)
            for r in (await _exec(session, sql_load, {"closed": "已结束"})).fetchall()}
    for s in pool:
        s["current_load"] = load.get(s["staff_id"], 0)

    considered = len(pool)
    free = [s for s in pool if s["current_load"] < s["max_concurrent"]]
    if not free:
        notes.append(f"{considered} 名候选人全部达到 max_concurrent，无人可分派")
        return None, mode, considered

    if _on(rules, "handoff.prefer_owner", False) and owner_staff:
        for s in free:
            if s["staff_id"] == owner_staff:
                return s, f"{mode}+优先归属员工", considered

    free.sort(key=lambda s: (s["current_load"], s["staff_id"]))
    return free[0], mode, considered


async def _brief_view(session, schema: str, tables: set, cust_id: str) -> dict:
    """精简客户全景。完整九槽应由调用方先调 read_view 传进来。"""
    out = {"_source": "handoff_pack 精简查询（非 read_view 完整九槽）"}
    if not cust_id or "crm_customers" not in tables:
        return out
    tbl = _tbl(schema, "crm_customers")
    cols = ", ".join(_qi(c) for c in ("cust_id", "name", "company", "status",
                                      "owner_staff", "do_not_touch"))
    row = (await _exec(session, f"SELECT {cols} FROM {tbl} "
                                f"WHERE {_qi('cust_id')} = :cid LIMIT 1",
                       {"cid": cust_id})).fetchone()
    if row:
        out.update({"cust_id": row[0], "name": row[1], "company": row[2],
                    "status": row[3], "owner_staff": row[4],
                    "do_not_touch": bool(row[5])})
    return out


async def _risk_bar(session, schema: str, tables: set, cust_id: str) -> list:
    """风险条：风险标签 + 钉选记忆。标签永远压制记忆和画像。"""
    items = []
    if not cust_id:
        return items
    if "crm_customer_tags" in tables:
        tbl = _tbl(schema, "crm_customer_tags")
        cols = ", ".join(_qi(c) for c in ("tag_name", "tag_value", "reason"))
        sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
               f"AND {_qi('state')} = :st "
               f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE")
        for r in (await _exec(session, sql, {"cid": cust_id, "st": "生效"})).fetchall():
            if r[0] in _RISK_TAGS:
                items.append({"text": f"{r[0]}{('：' + r[1]) if r[1] else ''}",
                              "reason": r[2], "source": "crm_customer_tags"})
    if "crm_memories" in tables:
        tbl = _tbl(schema, "crm_memories")
        cols = ", ".join(_qi(c) for c in ("content", "evidence"))
        sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('cust_id')} = :cid "
               f"AND {_qi('mem_type')} = :mt AND {_qi('state')} = :st "
               f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE")
        for r in (await _exec(session, sql, {"cid": cust_id, "mt": "钉选", "st": "生效"})).fetchall():
            items.append({"text": r[0], "evidence": r[1],
                          "source": "crm_memories（钉选）"})
    return items


# --------------------------------------------------------------------------- 写
def _append_handled_by(existing: str, staff_id: str) -> str:
    """追加而非覆盖；已在列表里就不重复追加，保证重复调用幂等。"""
    parts = [p.strip() for p in str(existing or "").split(",") if p.strip()]
    if not parts:
        parts = ["AI"]
    if staff_id and staff_id not in parts:
        parts.append(staff_id)
    return ",".join(parts)


async def _assign_conv(session, schema: str, conv_id: str, handled_by: str,
                       assignee_id: str, reason: str, actor: str) -> int:
    tbl = _tbl(schema, "crm_conversations")
    sql = (f"UPDATE {tbl} SET {_qi('handled_by')} = :handled_by, "
           f"{_qi('current_assignee')} = :assignee, "
           f"{_qi('handoff_reason')} = :reason, {_qi('state')} = :state, "
           f"{_qi('updated_at')} = :now, {_qi('updated_by')} = :actor "
           f"WHERE {_qi('conv_id')} = :cid")
    res = await _exec(session, sql, {
        "handled_by": handled_by, "assignee": assignee_id or None,
        "reason": reason, "state": "等我回", "now": _now(),
        "actor": actor, "cid": conv_id})
    await session.commit()          # AsyncSession 不自动提交，漏了会静默回滚
    return int(res.rowcount or 0)


async def _log_activity(session, schema: str, conv_id: str, cust_id: str,
                        assignee_id: str, reason: str, actor: str) -> int:
    tbl = _tbl(schema, "crm_activities")
    row = {
        "act_key": f"AC{_now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}",
        "cust_id": cust_id or None, "conv_id": conv_id, "act_type": "系统事件",
        "content": json.dumps({"event": "handoff", "assignee": assignee_id,
                               "reason": reason}, ensure_ascii=False),
        "created_by": actor, "created_at": _now(),
    }
    cols = ", ".join(_qi(k) for k in row)
    binds = ", ".join(f":{k}" for k in row)
    res = await _exec(session, f"INSERT INTO {tbl} ({cols}) VALUES ({binds})", row)
    await session.commit()
    return int(res.rowcount or 1)


# --------------------------------------------------------------------------- 入口
async def execute(input_data: dict, runtime_context: dict) -> dict:
    """
    input_data:
      conv_id          必填，要转的会话
      reason           必填，转人工原因
      cust_id          缺省从会话行读
      intent_main      决定按 type_map 分到哪个组
      customer_view    read_view 的产物；不传则本技能做精简查询
      ai_attempts      [{tried, result}] AI 试过什么·为什么没成
      next_steps       建议下一步
      target_staff_id  指定接手人，跳过自动分派

    返回：assignee / packet / assign_mode_used / candidates_considered /
          written / customer_notified / skipped / notes；
          失败返回 {"error": "..."}，不抛异常。
    """
    rc = runtime_context or {}
    inp = input_data or {}
    notes: list = []
    skipped: list = []
    written = {"conversations": 0, "activities": 0}

    conv_id = str(inp.get("conv_id") or rc.get("conv_id") or "").strip()
    reason = str(inp.get("reason") or "").strip()
    missing = [k for k, v in (("conv_id", conv_id), ("reason", reason)) if not v]
    if missing:
        return {"error": f"缺少必填参数: {', '.join(missing)}"}

    session = _get_session(rc)
    if session is None:
        return {"error": "runtime_context 缺少 db（SQLAlchemy AsyncSession）"}
    schema = _org_schema(rc)
    if not schema:
        return {"error": "runtime_context 缺少 org_id，拒绝猜 schema"}

    rules = rc.get("rules") or {}
    actor = str(rc.get("staff_id") or rc.get("role_id") or "AI")

    try:
        tables = await _existing_tables(session, schema)
        lost = [t for t in _REQUIRED_TABLES if t not in tables]
        if lost:
            return {"error": f"必需表缺失: {', '.join(lost)}（schema={schema}）"}
        for t in _OPTIONAL_TABLES:
            if t not in tables:
                skipped.append({"table": t, "reason": "表不存在，跳过"})

        conv = await _load_conv(session, schema, conv_id)
        if not conv:
            return {"error": f"会话不存在或已软删: {conv_id}"}
        cust_id = str(inp.get("cust_id") or conv.get("cust_id") or "").strip()
        intent = str(inp.get("intent_main") or conv.get("intent_main") or "").strip()

        # 1) 选人：指定优先，否则按规则分派
        assignee, mode_used, considered = None, "指定", 0
        if inp.get("target_staff_id"):
            assignee = {"staff_id": str(inp["target_staff_id"]).strip(),
                        "name": None, "dept": None, "current_load": None}
        else:
            owner = ""
            if "crm_customers" in tables and cust_id:
                try:
                    tbl = _tbl(schema, "crm_customers")
                    r = (await _exec(session,
                                     f"SELECT {_qi('owner_staff')} FROM {tbl} "
                                     f"WHERE {_qi('cust_id')} = :cid LIMIT 1",
                                     {"cid": cust_id})).fetchone()
                    owner = (r[0] or "") if r else ""
                except Exception as e:                # noqa: BLE001 归属员工是可选输入
                    logger.exception("查归属员工失败: %r", e)
                    notes.append(f"查归属员工失败: {e}")
                    await session.rollback()
            try:
                assignee, mode_used, considered = await _pick_staff(
                    session, schema, rules, intent, owner, notes)
            except Exception as e:                    # noqa: BLE001 选人失败仍要出交接包
                logger.exception("分派选人失败: %r", e)
                notes.append(f"分派选人失败，会话转为待认领: {e}")
                await session.rollback()
                mode_used = str(_rule(rules, "handoff.assign_mode", "按问题类型"))

        # 2) 组交接包
        items = _as_list(_rule(rules, "handoff.packet_items", None), _PACKET_ITEMS)
        packet: dict = {}
        for item in items:
            if item not in _PACKET_ITEMS:
                notes.append(f"packet_items 含未知项 {item!r}，已忽略")
                continue
            try:
                if item == "customer_view":
                    packet[item] = inp.get("customer_view") or \
                        await _brief_view(session, schema, tables, cust_id)
                    if not inp.get("customer_view"):
                        notes.append("customer_view 未传入，用的是精简查询而非 read_view 完整九槽")
                elif item == "conv_summary":
                    packet[item] = {"summary": conv.get("summary"),
                                    "intent": intent, "emotion": conv.get("emotion"),
                                    "turn_count": conv.get("turn_count"),
                                    "started_at": str(conv.get("started_at") or "")}
                elif item == "ai_attempts":
                    packet[item] = list(inp.get("ai_attempts") or [])
                    if not packet[item]:
                        notes.append("ai_attempts 为空，接手人可能会重复问客户同样的问题")
                elif item == "next_steps":
                    packet[item] = list(inp.get("next_steps") or [])
                elif item == "history":
                    packet[item] = {"platform_thread_id": conv.get("platform_thread_id"),
                                    "conv_id": conv_id,
                                    "_note": "完整历史消息在平台消息流水，按 conv_id 拉取"}
                elif item == "risk_bar":
                    packet[item] = await _risk_bar(session, schema, tables, cust_id)
            except Exception as e:                    # noqa: BLE001 单件套隔离
                logger.exception("交接包 %s 组装失败: %r", item, e)
                notes.append(f"交接包 {item} 组装失败: {e}")
                packet[item] = None
                await session.rollback()

        # 3) 写会话归属（主数据）
        assignee_id = (assignee or {}).get("staff_id") or ""
        handled_by = _append_handled_by(conv.get("handled_by"), assignee_id)
        written["conversations"] = await _assign_conv(
            session, schema, conv_id, handled_by, assignee_id, reason, actor)

        # 4) 写台账（附属数据，失败不回滚会话归属）
        if "crm_activities" in tables:
            try:
                written["activities"] = await _log_activity(
                    session, schema, conv_id, cust_id, assignee_id, reason, actor)
            except Exception as e:                    # noqa: BLE001
                logger.exception("写 activities 台账失败: %r", e)
                notes.append(f"写台账失败（会话归属已生效）: {e}")
                await session.rollback()

        if not assignee:
            notes.append("无人可分派，会话已转为待认领状态，交接包照常生成")

        return {"assignee": assignee, "packet": packet, "packet_items": list(items),
                "assign_mode_used": mode_used, "candidates_considered": considered,
                "handled_by": handled_by, "written": written,
                "customer_notified": False,
                "no_transfer_words": list(_as_list(
                    _rule(rules, "handoff.no_transfer_words", None),
                    _DEFAULT_NO_TRANSFER_WORDS)),
                "skipped": skipped, "notes": notes}

    except Exception as e:                           # noqa: BLE001 入口兜底
        logger.exception("handoff_pack 失败: %r", e)
        try:
            await session.rollback()
        except Exception as re_:                     # noqa: BLE001
            logger.exception("rollback 失败: %r", re_)
        return {"error": str(e), "written": written,
                "skipped": skipped, "notes": notes}
