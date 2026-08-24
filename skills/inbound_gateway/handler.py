"""
skills/inbound_gateway/handler.py

S-01 收消息：各渠道消息统一收进来，判断归属哪一段会话，防重复投递。

设计要点：
  ① 纯规则零 token —— 全流程不调模型，规则值来自 runtime_context["rules"]。
  ② 幂等 —— platform_msg_id 命中去重窗口直接返回 is_duplicate=true，零写入，可重复触发。
  ③ 附属数据不阻断主数据 —— 每个附件各自 try/except + rollback，
     附件落库失败不影响会话主写入（施工规范第 9 条）。
  ④ 不跨技能 import —— 附件交给 media_process 的方式是返回 media_keys，
     由平台 skill_schedule 或角色按序调用，绝不 import 其它技能（第 13 条）。

runtime_context：{org_id, role_id, staff_id, conv_id, rules, db, models}
  db     平台注入的 SQLAlchemy AsyncSession（非 asyncpg 裸连），兼容 session / db_session
  rules  当前生效的规则值，来自 crm_rules

依赖：pip install sqlalchemy[asyncio] asyncpg
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger("myinc.skills.inbound_gateway")

_CHANNELS = ("官网", "微信", "企微", "公众号", "小程序", "抖音",
             "小红书", "淘宝", "邮件", "电话", "API")
_MEDIA_TYPES = ("图片", "视频", "语音", "文件", "位置")
_CONV_OPEN_STATES = ("进行中", "等客户", "等我回")

_REQUIRED_TABLES = ("crm_conversations",)
_OPTIONAL_TABLES = ("crm_media", "crm_identities")

# 会话 custom_fields 里维护的去重滑动窗口最多保留多少条
_RECENT_MSG_CAP = 200


# --------------------------------------------------------------------------- 会话/表定位
def _get_session(rc: dict):
    """平台注入 SQLAlchemy AsyncSession，不自己建连接。兼容三种键名。"""
    for key in ("db", "session", "db_session"):
        sess = (rc or {}).get(key)
        if sess is not None:
            return sess
    return None


def _org_schema(rc: dict) -> str:
    """业务表在 org_data_<org_id去横线>。取不到 org_id 要报错，别默默用错 schema。"""
    org_id = str((rc or {}).get("org_id") or "").replace("-", "").strip()
    return f"org_data_{org_id}" if org_id else ""


def _qi(name: str) -> str:
    """列名/表名一律加双引号：state / text / value 这类保留字裸写必炸。"""
    return '"' + str(name).replace('"', '""') + '"'


def _tbl(schema: str, table: str) -> str:
    return f"{schema}.{_qi(table)}"


_UNSAFE_CAST = re.compile(r":\w+::")


def _assert_safe_sql(sql: str) -> None:
    """静态防呆：asyncpg 下 :name::type 会解析崩，要转型只能用 cast(:x AS type)。"""
    if _UNSAFE_CAST.search(sql):
        raise ValueError(f"SQL 含 :name::type 写法，asyncpg 会解析失败: {sql[:120]}")


async def _exec(session, sql: str, params: dict | None = None):
    _assert_safe_sql(sql)
    return await session.execute(text(sql), params or {})


async def _existing_tables(session, schema: str) -> set:
    """一次性查 schema 有哪些表。对不存在的表发 SELECT 会中止整个事务，后续全连坐。"""
    sql = ("SELECT table_name FROM information_schema.tables "
           "WHERE table_schema = :schema")
    rows = await _exec(session, sql, {"schema": schema})
    return {r[0] for r in rows.fetchall()}


# --------------------------------------------------------------------------- 规则
def _rule(rules: dict, key: str, default, cast=None):
    """规则缺失或值非法时退回默认值，并不让一条坏规则炸掉整个技能。"""
    raw = (rules or {}).get(key, default)
    if cast is None:
        return raw
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("规则 %s 值非法(%r)，退回默认 %r", key, raw, default)
        return default


def _channels_enabled(rules: dict) -> tuple:
    raw = _rule(rules, "gateway.channels_enabled", None)
    if not raw or raw in ("全开", "all", "*"):
        return _CHANNELS
    if isinstance(raw, str):
        raw = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
    return tuple(raw) if raw else _CHANNELS


# --------------------------------------------------------------------------- 时间
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw) -> datetime:
    """入参时间戳解析失败不报错，退回当前时间——一条脏时间戳不该挡住收消息。"""
    if not raw:
        return _now()
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("ts 解析失败(%r)，用当前时间", raw)
        return _now()


def _aware(dt) -> datetime | None:
    """库里取出的 datetime 可能不带时区，统一按 UTC 处理再比较。"""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- 去重窗口
def _load_custom(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def _hit_dedup(custom: dict, msg_id: str, now: datetime, window_s: int) -> bool:
    """同 platform_msg_id 在窗口内出现过即算重复投递。"""
    cutoff = (now - timedelta(seconds=window_s)).timestamp()
    for item in custom.get("recent_msg_ids") or []:
        try:
            if item.get("id") == msg_id and float(item.get("at", 0)) >= cutoff:
                return True
        except (AttributeError, TypeError, ValueError):
            continue
    return False


def _push_msg_id(custom: dict, msg_id: str, now: datetime, window_s: int) -> dict:
    """写入新 msg_id 并裁掉过窗口的旧记录，避免 JSON 列无限膨胀。"""
    cutoff = (now - timedelta(seconds=window_s)).timestamp()
    kept = []
    for item in custom.get("recent_msg_ids") or []:
        try:
            if float(item.get("at", 0)) >= cutoff and item.get("id") != msg_id:
                kept.append(item)
        except (AttributeError, TypeError, ValueError):
            continue
    kept.append({"id": msg_id, "at": now.timestamp()})
    custom["recent_msg_ids"] = kept[-_RECENT_MSG_CAP:]
    return custom


# --------------------------------------------------------------------------- 读会话
async def _find_open_conv(session, schema: str, channel: str,
                          external_id: str, cust_id: str) -> dict | None:
    """
    找该客户在本渠道上未结束的最近一段会话。
    cust_id 已知时按 cust_id 找（跨渠道同一客户仍分渠道成段）；
    未知时退回 external_id —— 首轮消息 identity_resolve 还没跑，只能靠外部 ID。
    """
    tbl = _tbl(schema, "crm_conversations")
    cols = ", ".join(_qi(c) for c in (
        "conv_id", "cust_id", "state", "turn_count", "started_at",
        "updated_at", "custom_fields"))
    states = ", ".join(f":st{i}" for i in range(len(_CONV_OPEN_STATES)))
    params = {f"st{i}": s for i, s in enumerate(_CONV_OPEN_STATES)}
    params.update({"channel": channel})

    if cust_id:
        where = f"{_qi('cust_id')} = :cust_id"
        params["cust_id"] = cust_id
    else:
        where = f"{_qi('external_id')} = :external_id"
        params["external_id"] = external_id

    sql = (f"SELECT {cols} FROM {tbl} "
           f"WHERE {where} AND {_qi('channel')} = :channel "
           f"AND {_qi('state')} IN ({states}) "
           f"AND COALESCE({_qi('is_deleted')}, FALSE) = FALSE "
           f"ORDER BY {_qi('updated_at')} DESC NULLS LAST, {_qi('started_at')} DESC "
           f"LIMIT 1")
    rows = await _exec(session, sql, params)
    row = rows.fetchone()
    if not row:
        return None
    return {
        "conv_id": row[0], "cust_id": row[1], "state": row[2],
        "turn_count": int(row[3] or 0), "started_at": _aware(row[4]),
        "updated_at": _aware(row[5]), "custom_fields": _load_custom(row[6]),
    }


# --------------------------------------------------------------------------- 写会话
def _new_conv_id(now: datetime) -> str:
    return f"CV{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"


async def _create_conv(session, schema: str, conv: dict) -> None:
    tbl = _tbl(schema, "crm_conversations")
    fields = ["conv_id", "cust_id", "channel", "external_id", "platform_thread_id",
              "state", "turn_count", "started_at", "custom_fields",
              "created_by", "updated_by", "is_deleted", "created_at", "updated_at"]
    cols = ", ".join(_qi(f) for f in fields)
    binds = ", ".join(f":{f}" for f in fields)
    sql = f"INSERT INTO {tbl} ({cols}) VALUES ({binds})"
    await _exec(session, sql, conv)
    await session.commit()          # AsyncSession 不自动提交，漏了会静默回滚


async def _touch_conv(session, schema: str, conv_id: str,
                      turn_count: int, custom: dict, now: datetime,
                      updated_by: str) -> None:
    tbl = _tbl(schema, "crm_conversations")
    sql = (f"UPDATE {tbl} SET {_qi('turn_count')} = :turn_count, "
           f"{_qi('custom_fields')} = :custom_fields, "
           f"{_qi('updated_at')} = :updated_at, {_qi('updated_by')} = :updated_by "
           f"WHERE {_qi('conv_id')} = :conv_id")
    await _exec(session, sql, {
        "turn_count": turn_count,
        "custom_fields": json.dumps(custom, ensure_ascii=False),
        "updated_at": now, "updated_by": updated_by, "conv_id": conv_id,
    })
    await session.commit()


# --------------------------------------------------------------------------- 附件（附属数据）
async def _insert_media(session, schema: str, item: dict, conv_id: str,
                        cust_id: str, msg_id: str, now: datetime) -> str:
    """
    单个附件落库。ai_description / ai_extracted / check_result 留空，
    由下游 media_process 回填——本技能不识别内容，只登记。
    """
    media_key = f"MD{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}"
    mtype = str(item.get("media_type") or "文件").strip()
    if mtype not in _MEDIA_TYPES:
        mtype = "文件"
    row = {
        "media_key": media_key, "conv_id": conv_id, "cust_id": cust_id or None,
        "platform_msg_id": msg_id, "direction": "客户发来", "media_type": mtype,
        "file_name": item.get("file_name"), "file_size": item.get("file_size"),
        "duration_s": item.get("duration_s"),
        "storage_url": item.get("storage_url"),
        "thumbnail_url": item.get("thumbnail_url"), "sent_at": now,
    }
    tbl = _tbl(schema, "crm_media")
    cols = ", ".join(_qi(k) for k in row)
    binds = ", ".join(f":{k}" for k in row)
    await _exec(session, f"INSERT INTO {tbl} ({cols}) VALUES ({binds})", row)
    await session.commit()
    return media_key


async def _save_media(session, schema: str, media: list, conv_id: str,
                      cust_id: str, msg_id: str, now: datetime,
                      skipped: list, notes: list) -> list:
    """每个附件各自 try/except：一个坏附件不该炸掉整段会话的主写入。"""
    keys = []
    for idx, item in enumerate(media):
        if not isinstance(item, dict) or not item.get("storage_url"):
            skipped.append({"media_index": idx, "reason": "缺 storage_url"})
            continue
        try:
            keys.append(await _insert_media(
                session, schema, item, conv_id, cust_id, msg_id, now))
        except Exception as e:                       # noqa: BLE001 附属数据单点隔离
            logger.exception("附件落库失败 idx=%s: %r", idx, e)
            skipped.append({"media_index": idx, "reason": str(e)})
            notes.append(f"media[{idx}] 落库失败: {e}")
            try:
                await session.rollback()             # 脏事务不能连累后续
            except Exception as re_:                 # noqa: BLE001
                logger.exception("rollback 失败: %r", re_)
    return keys


# --------------------------------------------------------------------------- 入口
async def execute(input_data: dict, runtime_context: dict) -> dict:
    """
    input_data:
      channel          渠道，必填，须在 gateway.channels_enabled 内
      external_id      该渠道下的客户外部 ID，必填
      platform_msg_id  平台消息 ID，必填，去重靠它
      text             消息正文，超 gateway.max_msg_len 截断
      media            附件数组 [{media_type, storage_url, ...}]
      ts               ISO8601 时间戳，缺省取当前 UTC
      cust_id          已知客户则带上，未知留空由 identity_resolve 回填

    返回：conv_id / is_new_conv / is_duplicate / media_keys / turn_count /
          text_truncated / skipped / notes；失败返回 {"error": "..."}，不抛异常。
    """
    rc = runtime_context or {}
    inp = input_data or {}
    notes: list = []
    skipped: list = []

    # 1) 参数校验：缺参直接返回 error，不猜
    channel = str(inp.get("channel") or "").strip()
    external_id = str(inp.get("external_id") or "").strip()
    msg_id = str(inp.get("platform_msg_id") or "").strip()
    missing = [k for k, v in (("channel", channel), ("external_id", external_id),
                              ("platform_msg_id", msg_id)) if not v]
    if missing:
        return {"error": f"缺少必填参数: {', '.join(missing)}"}

    rules = rc.get("rules") or {}
    enabled = _channels_enabled(rules)
    if channel not in enabled:
        return {"error": f"渠道 {channel} 未启用（gateway.channels_enabled）",
                "enabled_channels": list(enabled)}

    session = _get_session(rc)
    if session is None:
        return {"error": "runtime_context 缺少 db（SQLAlchemy AsyncSession）"}
    schema = _org_schema(rc)
    if not schema:
        return {"error": "runtime_context 缺少 org_id，拒绝猜 schema"}

    dedup_window_s = _rule(rules, "gateway.dedup_window_s", 300, int)
    gap_min = _rule(rules, "gateway.new_conv_gap_min", 30, int)
    max_len = _rule(rules, "gateway.max_msg_len", 4000, int)

    now = _parse_ts(inp.get("ts"))
    actor = str(rc.get("staff_id") or rc.get("role_id") or "system")
    cust_id = str(inp.get("cust_id") or "").strip()
    body = str(inp.get("text") or "")
    truncated = len(body) > max_len
    if truncated:
        body = body[:max_len]

    try:
        # 2) 不存在的表绝不发 SELECT
        tables = await _existing_tables(session, schema)
        lost_required = [t for t in _REQUIRED_TABLES if t not in tables]
        if lost_required:
            return {"error": f"必需表缺失: {', '.join(lost_required)}（schema={schema}）"}
        for t in _OPTIONAL_TABLES:
            if t not in tables:
                skipped.append({"table": t, "reason": "表不存在，跳过"})

        # 3) 找会话
        conv = await _find_open_conv(session, schema, channel, external_id, cust_id)
        if conv:
            last = conv["updated_at"] or conv["started_at"] or now
            if (now - last) > timedelta(minutes=gap_min):
                conv = None          # 超过间隔，这段算结束了，另开新段

        # 4) 幂等：命中去重窗口 → 零写入返回
        if conv and _hit_dedup(conv["custom_fields"], msg_id, now, dedup_window_s):
            return {"conv_id": conv["conv_id"], "is_new_conv": False,
                    "is_duplicate": True, "media_keys": [],
                    "turn_count": conv["turn_count"], "text_truncated": truncated,
                    "skipped": skipped, "notes": notes,
                    "note": f"platform_msg_id 在 {dedup_window_s}s 窗口内重复，未写入"}

        # 5) 复用或新建会话
        if conv:
            conv_id = conv["conv_id"]
            is_new = False
            turn_count = conv["turn_count"] + 1
            custom = _push_msg_id(conv["custom_fields"], msg_id, now, dedup_window_s)
            await _touch_conv(session, schema, conv_id, turn_count, custom, now, actor)
            cust_id = cust_id or (conv["cust_id"] or "")
        else:
            conv_id = _new_conv_id(now)
            is_new = True
            turn_count = 0           # 手册：新建会话 turn_count=0
            custom = _push_msg_id({}, msg_id, now, dedup_window_s)
            await _create_conv(session, schema, {
                "conv_id": conv_id, "cust_id": cust_id or None, "channel": channel,
                "external_id": external_id,
                "platform_thread_id": inp.get("platform_thread_id"),
                "state": "进行中", "turn_count": turn_count, "started_at": now,
                "custom_fields": json.dumps(custom, ensure_ascii=False),
                "created_by": actor, "updated_by": actor, "is_deleted": False,
                "created_at": now, "updated_at": now,
            })

        # 6) 附件：附属数据，失败不阻断主流程
        media = inp.get("media") or []
        media_keys = []
        if media and "crm_media" in tables:
            media_keys = await _save_media(session, schema, media, conv_id,
                                           cust_id, msg_id, now, skipped, notes)
        elif media:
            skipped.append({"media_count": len(media), "reason": "crm_media 表不存在"})

        return {"conv_id": conv_id, "is_new_conv": is_new, "is_duplicate": False,
                "media_keys": media_keys, "turn_count": turn_count,
                "text_truncated": truncated, "text_len": len(body),
                "cust_id": cust_id or None, "skipped": skipped, "notes": notes,
                "ts": now.isoformat()}

    except Exception as e:                           # noqa: BLE001 入口兜底
        logger.exception("inbound_gateway 失败: %r", e)
        try:
            await session.rollback()
        except Exception as re_:                     # noqa: BLE001
            logger.exception("rollback 失败: %r", re_)
        return {"error": str(e), "skipped": skipped, "notes": notes}
