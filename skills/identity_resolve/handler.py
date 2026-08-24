"""
skills/identity_resolve/handler.py

S-02 认客户：拿渠道 ID 找到人；找不到按规则建线索或客户；合并只在强证据下自动做。

设计要点：
  ① 匹配优先级固定 —— identity_key → phone → unionid → self_claim → email(默认关)
     → addr_name(默认关) → 弱证据(只标待确认) → 新建。命中即返回，不再往下试，
     避免一条弱证据覆盖强证据的结论。这个顺序不做成规则，因为它直接决定错并风险。
  ② 弱证据绝不自动挂到已有 cust_id 上 —— 把两个人错认成一个，等于把 A 的订单和
     聊天记录暴露在 B 的服务界面上。弱证据只建 merge_state=待确认 的 identity。
  ③ 合并可撤销 —— 合并前把双方主档快照写进 merge_evidence，撤销全靠它。
  ④ 不假设唯一约束存在 —— 平台建表器会剥 PRIMARY KEY，一律先 SELECT 再 INSERT，
     客户编号冲突重试 3 次（施工规范第 7 条）。

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

logger = logging.getLogger("myinc.skills.identity_resolve")

_REQUIRED_TABLES = ("crm_identities", "crm_customers")
_OPTIONAL_TABLES = ("crm_leads", "crm_conversations")

# 各证据的把握度。强证据 ≥0.9 才允许自动合并，弱证据封顶 0.6 只能进待确认队列。
_CONFIDENCE = {
    "identity_key": 1.0, "phone": 0.95, "unionid": 0.95, "self_claim": 0.90,
    "email": 0.85, "addr_name": 0.80, "weak": 0.55, "new": 1.0,
}
_ID_RETRY = 3

# 渠道别名：同一个渠道在平台侧可能叫 web / 官网 / official，
# identity_key 是字面拼接，命名不统一就会出现"数据在位却读不到"。
# 精确匹配永远优先，这张表只在精确未命中时兜底，并且会告警而不是静默修正。
_CHANNEL_ALIASES = {
    "官网": ("官网", "web", "website", "official", "pc", "site"),
    "微信": ("微信", "wechat", "weixin", "wx"),
    "企微": ("企微", "企业微信", "wecom", "qywx"),
    "公众号": ("公众号", "mp", "oa", "offiaccount"),
    "小程序": ("小程序", "miniprogram", "mini", "wxapp", "applet"),
    "抖音": ("抖音", "douyin", "tiktok", "dy"),
    "小红书": ("小红书", "xiaohongshu", "xhs", "rednote"),
    "淘宝": ("淘宝", "taobao", "tb", "tmall", "天猫"),
    "邮件": ("邮件", "email", "mail"),
    "电话": ("电话", "phone", "tel", "call"),
    "API": ("api", "openapi"),
}
_ALIAS_TO_CANON = {a.lower(): canon
                   for canon, aliases in _CHANNEL_ALIASES.items()
                   for a in aliases}


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
    """规则开关：兼容 开/关、true/false、1/0 几种写法。"""
    raw = _rule(rules, key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("开", "true", "1", "yes", "on", "启用")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_phone(raw) -> str:
    """只留数字再比对：+86 138-0000-0000 和 13800000000 是同一个号。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits[-11:] if len(digits) > 11 else digits


# --------------------------------------------------------------------------- 读
async def _find_identity(session, schema: str, identity_key: str) -> dict | None:
    tbl = _tbl(schema, "crm_identities")
    cols = ", ".join(_qi(c) for c in ("identity_key", "cust_id", "merge_state", "confidence"))
    sql = f"SELECT {cols} FROM {tbl} WHERE {_qi('identity_key')} = :k LIMIT 1"
    row = (await _exec(session, sql, {"k": identity_key})).fetchone()
    if not row:
        return None
    return {"identity_key": row[0], "cust_id": row[1],
            "merge_state": row[2], "confidence": float(row[3] or 0)}


def _canon_channel(channel: str) -> str:
    """把渠道值归一到手册枚举。认不出就原样返回，不猜。"""
    return _ALIAS_TO_CANON.get(str(channel or "").strip().lower(), str(channel or "").strip())


async def _find_identity_relaxed(session, schema: str, channel: str,
                                 external_id: str) -> dict | None:
    """
    精确 identity_key 未命中时的兜底：直接按 channel / external_id 两列比对，
    忽略大小写与首尾空格，并把渠道别名一并纳入。

    为什么需要它：identity_key 是 f"{channel}:{external_id}" 字面拼接，
    平台侧渠道叫 web 而库里存"官网"，或 external_id 带了不可见空格，
    都会让映射明明在位却读不到，表现为"把老客户当新客户重新核身"。

    命中后调用方必须告警——这是数据不一致的信号，不能静默修正了事。
    """
    canon = _canon_channel(channel)
    aliases = {a.lower() for a in _CHANNEL_ALIASES.get(canon, ())}
    aliases.add(str(channel or "").strip().lower())
    aliases.add(canon.lower())
    aliases.discard("")
    if not aliases:
        return None

    tbl = _tbl(schema, "crm_identities")
    cols = ", ".join(_qi(c) for c in
                     ("identity_key", "cust_id", "merge_state", "confidence", "channel"))
    binds = ", ".join(f":ch{i}" for i in range(len(aliases)))
    params = {f"ch{i}": a for i, a in enumerate(sorted(aliases))}
    params["ext"] = str(external_id or "").strip().lower()
    sql = (f"SELECT {cols} FROM {tbl} "
           f"WHERE lower(btrim({_qi('external_id')})) = :ext "
           f"AND lower(btrim({_qi('channel')})) IN ({binds}) "
           f"AND {_qi('cust_id')} IS NOT NULL LIMIT 1")
    row = (await _exec(session, sql, params)).fetchone()
    if not row:
        return None
    return {"identity_key": row[0], "cust_id": row[1], "merge_state": row[2],
            "confidence": float(row[3] or 0), "stored_channel": row[4]}


async def _find_customer_by(session, schema: str, column: str, value: str) -> dict | None:
    """按单列精确找客户。软删的不算——软删只是不显示，但不该再被匹配上。"""
    tbl = _tbl(schema, "crm_customers")
    cols = ", ".join(_qi(c) for c in ("cust_id", "name", "phone", "email", "status"))
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi(column)} = :v "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE LIMIT 1")
    row = (await _exec(session, sql, {"v": value})).fetchone()
    if not row:
        return None
    return {"cust_id": row[0], "name": row[1], "phone": row[2],
            "email": row[3], "status": row[4]}


async def _find_customer_by_phone(session, schema: str, phone: str) -> dict | None:
    """
    手机号可能带 +86 / 分隔符，库里存法不统一。
    先精确命中，未中再取候选集在 Python 里归一化比对——
    归一化写进 SQL 需要函数索引，平台建表器不保证有，宁可多读一次。
    """
    exact = await _find_customer_by(session, schema, "phone", phone)
    if exact:
        return exact
    target = _norm_phone(phone)
    if len(target) < 7:
        return None
    tbl = _tbl(schema, "crm_customers")
    cols = ", ".join(_qi(c) for c in ("cust_id", "name", "phone", "email", "status"))
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('phone')} IS NOT NULL "
           f"AND {_qi('phone')} LIKE :suffix "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE LIMIT 20")
    rows = (await _exec(session, sql, {"suffix": f"%{target[-8:]}"})).fetchall()
    for r in rows:
        if _norm_phone(r[2]) == target:
            return {"cust_id": r[0], "name": r[1], "phone": r[2],
                    "email": r[3], "status": r[4]}
    return None


async def _find_weak_candidates(session, schema: str, weak: dict) -> list:
    """弱证据只用来产出候选清单给人确认，绝不据此合并。"""
    name = str(weak.get("display_name") or "").strip()
    if not name or len(name) < 2:
        return []
    tbl = _tbl(schema, "crm_customers")
    cols = ", ".join(_qi(c) for c in ("cust_id", "name", "region", "industry"))
    sql = (f"SELECT {cols} FROM {tbl} WHERE {_qi('name')} ILIKE :like "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE LIMIT 5")
    rows = (await _exec(session, sql, {"like": f"%{name}%"})).fetchall()
    out = []
    for r in rows:
        hits = ["昵称相似"]
        if weak.get("region") and weak["region"] == r[2]:
            hits.append("同地区")
        if weak.get("industry") and weak["industry"] == r[3]:
            hits.append("同行业")
        out.append({"cust_id": r[0], "name": r[1], "hits": hits})
    return out


# --------------------------------------------------------------------------- 写
async def _next_serial_id(session, schema: str, table: str, col: str, prefix: str) -> str:
    """
    生成 C202607-000128 形式的编号：查当月最大序号 +1。
    不依赖数据库序列——平台建表器不保证建，也不假设唯一约束存在。
    """
    tbl = _tbl(schema, table)
    sql = (f"SELECT {_qi(col)} FROM {tbl} WHERE {_qi(col)} LIKE :p "
           f"ORDER BY {_qi(col)} DESC LIMIT 1")
    row = (await _exec(session, sql, {"p": f"{prefix}-%"})).fetchone()
    seq = 1
    if row and row[0]:
        m = re.search(r"-(\d+)$", str(row[0]))
        if m:
            seq = int(m.group(1)) + 1
    return f"{prefix}-{seq:06d}"


async def _insert_row(session, schema: str, table: str, row: dict) -> None:
    tbl = _tbl(schema, table)
    cols = ", ".join(_qi(k) for k in row)
    binds = ", ".join(f":{k}" for k in row)
    await _exec(session, f"INSERT INTO {tbl} ({cols}) VALUES ({binds})", row)
    await session.commit()          # AsyncSession 不自动提交，漏了会静默回滚


async def _create_principal(session, schema: str, table: str, col: str,
                            prefix: str, base: dict, notes: list) -> str:
    """
    新建客户或线索，编号冲突重试。
    先查后写而非 ON CONFLICT：唯一约束未必存在，冲突要靠重试兜底。
    """
    last_err = None
    for attempt in range(_ID_RETRY):
        new_id = await _next_serial_id(session, schema, table, col, prefix)
        exists = await _find_customer_by(session, schema, col, new_id) \
            if table == "crm_customers" else None
        if exists:
            continue
        try:
            await _insert_row(session, schema, table, {col: new_id, **base})
            return new_id
        except Exception as e:                       # noqa: BLE001 编号撞车重试
            last_err = e
            logger.warning("编号 %s 写入冲突，第 %s 次重试: %r", new_id, attempt + 1, e)
            try:
                await session.rollback()
            except Exception as re_:                 # noqa: BLE001
                logger.exception("rollback 失败: %r", re_)
    raise RuntimeError(f"{table} 编号连续 {_ID_RETRY} 次冲突，放弃: {last_err}")


async def _snapshot(session, schema: str, cust_id: str) -> dict:
    """合并前的主档快照，撤销全靠它。取不到就记空快照并说明，不静默跳过。"""
    row = await _find_customer_by(session, schema, "cust_id", cust_id)
    return row or {"cust_id": cust_id, "_note": "快照时未查到主档"}


async def _backfill_conv(session, schema: str, conv_id: str, cust_id: str) -> int:
    """返回实际影响行数。0 行意味着 conv_id 对不上——静默当成功会让上层永远发现不了。"""
    tbl = _tbl(schema, "crm_conversations")
    sql = (f"UPDATE {tbl} SET {_qi('cust_id')} = :cust_id, {_qi('updated_at')} = :now "
           f"WHERE {_qi('conv_id')} = :conv_id")
    res = await _exec(session, sql, {"cust_id": cust_id, "now": _now(), "conv_id": conv_id})
    await session.commit()
    return int(res.rowcount or 0)


# --------------------------------------------------------------------------- 强证据匹配
async def _try_strong(session, schema: str, inp: dict, rules: dict,
                      notes: list) -> tuple:
    """按固定优先级逐条试强证据，命中即返回 (matched_by, customer)。"""
    phone = str(inp.get("phone") or "").strip()
    if phone and _on(rules, "identity.merge_by_phone", True):
        cust = await _find_customer_by_phone(session, schema, phone)
        if cust:
            return "phone", cust

    unionid = str(inp.get("unionid") or "").strip()
    if unionid and _on(rules, "identity.merge_by_unionid", True):
        ident = await _find_identity(session, schema, f"unionid:{unionid}")
        if ident and ident.get("cust_id"):
            cust = await _find_customer_by(session, schema, "cust_id", ident["cust_id"])
            if cust:
                return "unionid", cust

    claim = inp.get("self_claim") or {}
    if isinstance(claim, dict) and claim.get("external_id") \
            and _on(rules, "identity.merge_by_self_claim", True):
        key = f"{claim.get('channel', '')}:{claim['external_id']}"
        ident = await _find_identity(session, schema, key)
        if ident and ident.get("cust_id"):
            cust = await _find_customer_by(session, schema, "cust_id", ident["cust_id"])
            if cust:
                return "self_claim", cust

    email = str(inp.get("email") or "").strip().lower()
    if email:
        if _on(rules, "identity.merge_by_email", False):
            cust = await _find_customer_by(session, schema, "email", email)
            if cust:
                return "email", cust
        else:
            notes.append("邮箱证据未启用（identity.merge_by_email 默认关，共用邮箱风险）")

    addr = inp.get("addr_name") or {}
    if isinstance(addr, dict) and addr.get("name") and addr.get("address"):
        if _on(rules, "identity.merge_by_addr_name", False):
            cust = await _find_customer_by(session, schema, "name", addr["name"])
            if cust:
                return "addr_name", cust
        else:
            notes.append("地址+姓名证据未启用（identity.merge_by_addr_name 默认关，同住家人风险）")

    return "", None


# --------------------------------------------------------------------------- 入口
async def execute(input_data: dict, runtime_context: dict) -> dict:
    """
    input_data:
      channel / external_id     必填，构成 identity_key
      phone / unionid / self_claim / email / addr_name   强证据（后两者默认关）
      weak_signals              弱证据 {display_name, region, industry, avatar_hash}
      name                      新建时的称呼
      conv_id                   回填会话归属用

    返回：cust_id / lead_id / matched_by / confidence / merge_state / is_new /
          merge_evidence_key / candidates / skipped / notes；
          失败返回 {"error": "..."}，不抛异常。
    """
    rc = runtime_context or {}
    inp = input_data or {}
    notes: list = []
    skipped: list = []

    channel = str(inp.get("channel") or "").strip()
    external_id = str(inp.get("external_id") or "").strip()
    if not channel or not external_id:
        return {"error": "缺少必填参数: channel / external_id"}

    session = _get_session(rc)
    if session is None:
        return {"error": "runtime_context 缺少 db（SQLAlchemy AsyncSession）"}
    schema = _org_schema(rc)
    if not schema:
        return {"error": "runtime_context 缺少 org_id，拒绝猜 schema"}

    rules = rc.get("rules") or {}
    now = _now()
    actor = str(rc.get("staff_id") or rc.get("role_id") or "system")
    identity_key = f"{channel}:{external_id}"
    conv_id = str(inp.get("conv_id") or rc.get("conv_id") or "").strip()

    try:
        tables = await _existing_tables(session, schema)
        lost = [t for t in _REQUIRED_TABLES if t not in tables]
        if lost:
            return {"error": f"必需表缺失: {', '.join(lost)}（schema={schema}）"}
        for t in _OPTIONAL_TABLES:
            if t not in tables:
                skipped.append({"table": t, "reason": "表不存在，跳过"})

        # 1) identity_key 精确命中 —— 99% 的消息走这条
        matched_how = "identity_key"
        ident = await _find_identity(session, schema, identity_key)

        # 1.5) 精确未命中时按 channel/external_id 两列宽松再找一次。
        #      映射明明在位却读不到（渠道叫 web 而库里存"官网"、external_id 带空格），
        #      表现就是把老客户当新客户重新核身。兜底救当次对话，但必须告警。
        if not (ident and ident.get("cust_id")):
            relaxed = await _find_identity_relaxed(session, schema, channel, external_id)
            if relaxed:
                ident = relaxed
                matched_how = "identity_key_relaxed"
                notes.append(
                    f"identity_key 字面未命中但按 channel/external_id 找到了："
                    f"传入 {identity_key!r} vs 库里 {relaxed['identity_key']!r}"
                    f"（库中 channel={relaxed.get('stored_channel')!r}）。"
                    f"这是数据不一致，请统一渠道命名后重测，不要依赖本兜底。")

        if ident and ident.get("cust_id"):
            backfilled, backfill_error = None, None
            if conv_id and "crm_conversations" in tables:
                try:
                    rows = await _backfill_conv(session, schema, conv_id, ident["cust_id"])
                    backfilled = rows
                    if rows == 0:
                        backfill_error = f"conv_id {conv_id} 未匹配到任何会话行"
                        notes.append(f"回填影响 0 行：{backfill_error}")
                except Exception as e:               # noqa: BLE001 回填失败不该吞
                    logger.exception("回填会话 cust_id 失败: %r", e)
                    backfill_error = str(e)
                    notes.append(f"会话回填失败: {e}")
                    await session.rollback()
            return {"cust_id": ident["cust_id"], "lead_id": None,
                    "matched_by": matched_how,
                    "confidence": _CONFIDENCE["identity_key"],
                    "merge_state": ident.get("merge_state") or "已确认",
                    "is_new": False, "merge_evidence_key": None,
                    "candidates": [], "conv_backfilled": backfilled,
                    "backfill_error": backfill_error,
                    "skipped": skipped, "notes": notes}

        # 2) 强证据逐条试
        matched_by, cust = await _try_strong(session, schema, inp, rules, notes)

        # 3) 强证据命中 → 建 identity 挂上去，合并前留快照
        if cust:
            reversible = _on(rules, "identity.merge_reversible", True)
            evidence = {"matched_by": matched_by, "at": now.isoformat(), "by": actor,
                        "incoming": {"channel": channel, "external_id": external_id,
                                     "name": inp.get("name")}}
            evidence_key = None
            if reversible:
                evidence_key = f"MG{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"
                evidence["merge_evidence_key"] = evidence_key
                evidence["snapshot_target"] = await _snapshot(session, schema, cust["cust_id"])
            else:
                notes.append("identity.merge_reversible 已关，本次合并不可撤销")

            await _insert_row(session, schema, "crm_identities", {
                "identity_key": identity_key, "cust_id": cust["cust_id"],
                "channel": channel, "external_id": external_id,
                "display_name": inp.get("name") or (inp.get("weak_signals") or {}).get("display_name"),
                "merge_state": "已确认",
                "merge_evidence": json.dumps(evidence, ensure_ascii=False),
                "confidence": _CONFIDENCE.get(matched_by, 0.9), "created_at": now,
            })
            backfilled, backfill_error = None, None
            if conv_id and "crm_conversations" in tables:
                try:
                    backfilled = await _backfill_conv(
                        session, schema, conv_id, cust["cust_id"])
                    if backfilled == 0:
                        backfill_error = f"conv_id {conv_id} 未匹配到任何会话行"
                        notes.append(f"回填影响 0 行：{backfill_error}")
                except Exception as e:               # noqa: BLE001
                    logger.exception("回填会话 cust_id 失败: %r", e)
                    backfill_error = str(e)
                    notes.append(f"会话回填失败: {e}")
                    await session.rollback()
            return {"cust_id": cust["cust_id"], "lead_id": None,
                    "matched_by": matched_by,
                    "confidence": _CONFIDENCE.get(matched_by, 0.9),
                    "merge_state": "已确认", "is_new": False,
                    "merge_evidence_key": evidence_key, "candidates": [],
                    "conv_backfilled": backfilled, "backfill_error": backfill_error,
                    "skipped": skipped, "notes": notes}

        # 4) 只有弱证据 → 建待确认 identity，绝不挂到已有 cust_id 上
        weak = inp.get("weak_signals") or {}
        candidates = []
        if weak and _on(rules, "identity.weak_to_pending", True):
            try:
                candidates = await _find_weak_candidates(session, schema, weak)
            except Exception as e:                   # noqa: BLE001 候选查询失败不阻断建档
                logger.exception("弱证据候选查询失败: %r", e)
                notes.append(f"弱证据候选查询失败: {e}")
                await session.rollback()
        if candidates:
            await _insert_row(session, schema, "crm_identities", {
                "identity_key": identity_key, "cust_id": None, "channel": channel,
                "external_id": external_id, "display_name": weak.get("display_name"),
                "merge_state": "待确认",
                "merge_evidence": json.dumps(
                    {"weak_signals": weak, "candidates": candidates,
                     "at": now.isoformat(),
                     "remind_after_days": _rule(rules, "identity.pending_remind_days", 3, int)},
                    ensure_ascii=False),
                "confidence": _CONFIDENCE["weak"], "created_at": now,
            })
            return {"cust_id": None, "lead_id": None, "matched_by": "weak",
                    "confidence": _CONFIDENCE["weak"], "merge_state": "待确认",
                    "is_new": False, "merge_evidence_key": None,
                    "candidates": candidates, "skipped": skipped,
                    "notes": notes + ["弱证据只挂待确认，未合并到任何已有客户"]}

        # 5) 全不命中 → 建线索或客户
        as_lead = _on(rules, "identity.new_as_lead", False)
        if as_lead and "crm_leads" not in tables:
            as_lead = False
            notes.append("identity.new_as_lead 已开但 crm_leads 表不存在，降级为建 customers")

        display = inp.get("name") or weak.get("display_name") or f"{channel}用户"
        month = now.strftime("%Y%m")
        if as_lead:
            lead_id = await _create_principal(
                session, schema, "crm_leads", "lead_id", f"L{month}",
                {"name": display, "channel": channel, "external_id": external_id,
                 "state": "新线索", "created_by": actor, "updated_by": actor,
                 "is_deleted": False, "created_at": now, "updated_at": now}, notes)
            await _insert_row(session, schema, "crm_identities", {
                "identity_key": identity_key, "cust_id": None, "channel": channel,
                "external_id": external_id, "display_name": display,
                "merge_state": "已确认",
                "merge_evidence": json.dumps(
                    {"new_as_lead": True, "lead_id": lead_id, "at": now.isoformat()},
                    ensure_ascii=False),
                "confidence": _CONFIDENCE["new"], "created_at": now,
            })
            return {"cust_id": None, "lead_id": lead_id, "matched_by": "new",
                    "confidence": _CONFIDENCE["new"], "merge_state": "已确认",
                    "is_new": True, "merge_evidence_key": None, "candidates": [],
                    "skipped": skipped, "notes": notes}

        cust_id = await _create_principal(
            session, schema, "crm_customers", "cust_id", f"C{month}",
            {"name": display, "cust_type": "个人", "status": "正常",
             "data_source": "客服会话", "audit_state": "已生效",
             "source_channel": channel,
             "phone": str(inp.get("phone") or "").strip() or None,
             "first_seen_at": now, "last_seen_at": now,
             "created_by": actor, "updated_by": actor, "is_deleted": False,
             "created_at": now, "updated_at": now}, notes)
        await _insert_row(session, schema, "crm_identities", {
            "identity_key": identity_key, "cust_id": cust_id, "channel": channel,
            "external_id": external_id, "display_name": display,
            "merge_state": "已确认",
            "merge_evidence": json.dumps(
                {"new_customer": True, "at": now.isoformat(), "by": actor},
                ensure_ascii=False),
            "confidence": _CONFIDENCE["new"], "created_at": now,
        })
        backfilled, backfill_error = None, None
        if conv_id and "crm_conversations" in tables:
            try:
                backfilled = await _backfill_conv(session, schema, conv_id, cust_id)
                if backfilled == 0:
                    backfill_error = f"conv_id {conv_id} 未匹配到任何会话行"
                    notes.append(f"回填影响 0 行：{backfill_error}")
            except Exception as e:                   # noqa: BLE001
                logger.exception("回填会话 cust_id 失败: %r", e)
                backfill_error = str(e)
                notes.append(f"会话回填失败: {e}")
                await session.rollback()

        return {"cust_id": cust_id, "lead_id": None, "matched_by": "new",
                "confidence": _CONFIDENCE["new"], "merge_state": "已确认",
                "is_new": True, "merge_evidence_key": None, "candidates": [],
                "conv_backfilled": backfilled, "backfill_error": backfill_error,
                "skipped": skipped, "notes": notes}

    except Exception as e:                           # noqa: BLE001 入口兜底
        logger.exception("identity_resolve 失败: %r", e)
        try:
            await session.rollback()
        except Exception as re_:                     # noqa: BLE001
            logger.exception("rollback 失败: %r", re_)
        return {"error": str(e), "skipped": skipped, "notes": notes}
