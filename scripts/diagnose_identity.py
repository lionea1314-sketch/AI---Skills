#!/usr/bin/env python3
"""身份识别链路断点定位 —— 一键版。

回答一个问题：客户明明是老客户，为什么系统把他当新人重新核身？

它不是技能，智能体不会调用它。这是排查工具，你（或运维）在命令行里跑，
它自己连库、自己跑五层检查、直接告诉你断在哪一层、该怎么修。

连不上数据库、或者不想碰命令行：改用 scripts/diagnose_identity_prompts.md，
那是同一套排查的自然语言指令版，直接在平台对话框里发给系统就行。

用法：
    python3 scripts/diagnose_identity.py \\
        --dsn "postgresql+asyncpg://user:pass@host:5432/dbname" \\
        --org-id "你的org_id" \\
        --channel "官网" --external-id "zhangwei"

    # DSN 也可以放环境变量，省得每次敲
    export CRM_DSN="postgresql+asyncpg://..."
    python3 scripts/diagnose_identity.py --org-id xxx --channel 官网 --external-id zhangwei

不知道 org_id 或 schema 叫什么：先跑 --list-schemas 看有哪些。

依赖：pip install "sqlalchemy[asyncio]" asyncpg
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

try:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
except ImportError:
    print('缺依赖，先跑：pip install "sqlalchemy[asyncio]" asyncpg')
    sys.exit(2)

OK, BAD, WARN, INFO = "✓", "✗", "!", "·"

# 与 identity_resolve/handler.py 保持一致的渠道别名
CHANNEL_ALIASES = {
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
ALIAS_TO_CANON = {a.lower(): c for c, al in CHANNEL_ALIASES.items() for a in al}


def qi(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def say(mark: str, msg: str, indent: int = 2) -> None:
    print(" " * indent + f"{mark} {msg}")


class Diagnosis:
    """把结论攒起来，最后一次性给修复建议——边跑边下结论容易前后打架。"""

    def __init__(self) -> None:
        self.broken_at: str | None = None
        self.cause: str | None = None
        self.fixes: list[str] = []

    def fail(self, layer: str, cause: str, *fixes: str) -> None:
        if self.broken_at is None:          # 只记第一个断点，后面的都是它的后果
            self.broken_at, self.cause = layer, cause
            self.fixes.extend(fixes)


async def list_schemas(conn) -> list:
    rows = await conn.execute(text(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name LIKE 'org_data_%' ORDER BY schema_name"))
    return [r[0] for r in rows.fetchall()]


async def tables_in(conn, schema: str) -> set:
    rows = await conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = :s"), {"s": schema})
    return {r[0] for r in rows.fetchall()}


async def layer0_identity(conn, schema, channel, ext, dg) -> str | None:
    """第 0 层：映射在不在？key 字面对不对得上？"""
    print("\n[第 0 层] crm_identities 里的映射")
    tbl = f"{schema}.{qi('crm_identities')}"
    expected = f"{channel}:{ext}"

    rows = (await conn.execute(text(
        f"SELECT {qi('identity_key')}, {qi('cust_id')}, {qi('merge_state')}, "
        f"{qi('channel')}, {qi('external_id')}, length({qi('identity_key')}), "
        f"encode(convert_to({qi('identity_key')}, 'UTF8'), 'hex') "
        f"FROM {tbl} WHERE lower(btrim({qi('external_id')})) = :ext LIMIT 10"),
        {"ext": ext.strip().lower()})).fetchall()

    if not rows:
        say(BAD, f"按 external_id={ext!r} 一条映射都查不到")
        dg.fail("第 0 层 · 映射不存在",
                f"crm_identities 里没有 external_id={ext!r} 的记录",
                f"平台侧把 {channel}:{ext} 绑定到对应 cust_id，写入 crm_identities",
                "绑定后重测；这一层不通，后面几层查了也没意义")
        return None

    exact = None
    for r in rows:
        key, cust, state, ch, _, klen, khex = r
        hit = key == expected
        say(OK if hit else WARN,
            f"identity_key={key!r} → cust_id={cust!r}（{state}，channel={ch!r}）")
        if not hit:
            say(INFO, f"字面不等：传入 {expected!r} vs 库里 {key!r}"
                      f"（长度 {klen}，hex 前 40: {khex[:40]}）", indent=6)
        if hit:
            exact = r

    if exact and exact[1]:
        say(OK, "精确命中，identity_resolve 第 1 步就该返回这个 cust_id")
        return exact[1]

    # 精确不中，看别名能不能救
    canon = ALIAS_TO_CANON.get(channel.strip().lower(), channel.strip())
    aliases = {a.lower() for a in CHANNEL_ALIASES.get(canon, ())} | {channel.strip().lower()}
    relaxed = [r for r in rows if str(r[3] or "").strip().lower() in aliases and r[1]]
    if relaxed:
        say(WARN, f"精确不中，但按渠道别名找到了：库里 channel={relaxed[0][3]!r}，"
                  f"你传的是 {channel!r}")
        dg.fail("第 0 层 · 渠道命名不一致",
                f"平台传 {channel!r}，库里存 {relaxed[0][3]!r}，identity_key 拼出来对不上",
                "短期：部署 identity_resolve 加固版（f296fc0），宽松兜底会认出来并告警",
                f"根治：统一渠道命名，把库里的 {relaxed[0][3]!r} 或平台侧的 {channel!r} 改成一致",
                "改完 matched_by 应回到 identity_key，不再是 identity_key_relaxed")
        return relaxed[0][1]

    say(BAD, "有 external_id 相同的记录，但渠道对不上，也不是已知别名")
    dg.fail("第 0 层 · 渠道对不上",
            f"传入渠道 {channel!r} 与库中记录的渠道都不匹配",
            "确认这个客户在该渠道下到底有没有身份记录")
    return None


async def layer1_conv(conn, schema, ext, conv_id, expect_cust, dg) -> None:
    """第 1 层：会话行有没有带上 cust_id。"""
    print("\n[第 1 层] crm_conversations 会话行")
    tbl = f"{schema}.{qi('crm_conversations')}"
    if conv_id:
        sql = (f"SELECT {qi('conv_id')}, {qi('cust_id')}, {qi('channel')}, "
               f"{qi('external_id')}, {qi('state')}, {qi('turn_count')}, {qi('started_at')} "
               f"FROM {tbl} WHERE {qi('conv_id')} = :c LIMIT 1")
        rows = (await conn.execute(text(sql), {"c": conv_id})).fetchall()
        if not rows:
            say(BAD, f"conv_id={conv_id!r} 查不到这条会话")
            dg.fail("第 1 层 · conv_id 对不上",
                    f"传给 identity_resolve 的 conv_id {conv_id!r} 在会话表里不存在",
                    "回填必然影响 0 行；加固版会把它报成 backfill_error")
            return
    else:
        rows = (await conn.execute(text(
            f"SELECT {qi('conv_id')}, {qi('cust_id')}, {qi('channel')}, "
            f"{qi('external_id')}, {qi('state')}, {qi('turn_count')}, {qi('started_at')} "
            f"FROM {tbl} WHERE lower(btrim(COALESCE({qi('external_id')}, ''))) = :e "
            f"ORDER BY {qi('started_at')} DESC NULLS LAST LIMIT 3"),
            {"e": ext.strip().lower()})).fetchall()
        if not rows:
            say(WARN, f"按 external_id={ext!r} 没找到会话（没传 --conv-id，只能这么找）")
            say(INFO, "若会话表的 external_id 存的不是渠道用户ID，这里查不到属正常", indent=6)
            return

    for r in rows:
        cid, cust, ch, e, st, turns, at = r
        if cust:
            say(OK, f"{cid} cust_id={cust!r} channel={ch!r} state={st} 轮数={turns}")
        else:
            say(BAD, f"{cid} cust_id 为空 channel={ch!r} state={st} 轮数={turns}")
            if expect_cust:
                dg.fail("第 1 层 · 回填没落到会话表",
                        f"第 0 层能查到 cust_id={expect_cust}，但会话 {cid} 的 cust_id 是空",
                        "可能一：identity_resolve 压根没被调用 → 查角色技能调用顺序",
                        "可能二：调用了但没传 conv_id → 回填无从执行",
                        "可能三：回填抛错被吞了 → 部署加固版，backfill_error 会显形")


async def layer2_called(conn, schema, dg) -> None:
    """第 2 层：identity_resolve 最近到底跑没跑过。"""
    print("\n[第 2 层] identity_resolve 近 24h 有没有执行痕迹")
    tbl = f"{schema}.{qi('crm_identities')}"
    rows = (await conn.execute(text(
        f"SELECT {qi('identity_key')}, {qi('merge_state')}, "
        f"left(COALESCE({qi('merge_evidence')}, ''), 120), {qi('created_at')} "
        f"FROM {tbl} WHERE {qi('created_at')} >= now() - interval '1 day' "
        f"ORDER BY {qi('created_at')} DESC LIMIT 10"))).fetchall()
    if not rows:
        say(WARN, "近 24h 无任何身份写入")
        say(INFO, "若这段时间做过测试，说明 identity_resolve 没被调用——"
                  "这是角色编排问题，不是技能问题", indent=6)
        return
    say(OK, f"近 24h 有 {len(rows)} 条身份写入，技能在跑")
    for key, state, ev, at in rows[:5]:
        flag = INFO
        if "new_customer" in (ev or ""):
            flag = WARN
        say(flag, f"{at} {key!r}（{state}）{ev[:60]}", indent=6)
    if any("new_customer" in (r[2] or "") for r in rows):
        say(WARN, "出现 new_customer=true：它没命中已有映射，反而新建了客户")


async def layer3_garbage(conn, schema, dg) -> None:
    """第 3 层：识别失败已经造了多少垃圾档案。"""
    print("\n[第 3 层] 近 24h 新建客户（识别失败的副作用）")
    tbl = f"{schema}.{qi('crm_customers')}"
    rows = (await conn.execute(text(
        f"SELECT {qi('cust_id')}, {qi('name')}, {qi('source_channel')}, {qi('created_at')} "
        f"FROM {tbl} WHERE {qi('created_at')} >= now() - interval '1 day' "
        f"AND COALESCE({qi('is_deleted')}, FALSE) = FALSE "
        f"ORDER BY {qi('created_at')} DESC LIMIT 20"))).fetchall()
    if not rows:
        say(OK, "近 24h 没有新建客户，没有产生垃圾档案")
        return
    auto = [r for r in rows if r[1] and str(r[1]).endswith("用户")]
    say(WARN if auto else INFO, f"近 24h 新建 {len(rows)} 个客户，其中 {len(auto)} 个是自动生成名")
    for cust, name, ch, at in rows[:8]:
        say(WARN if (name or "").endswith("用户") else INFO,
            f"{cust} {name!r} 来源={ch!r} {at}", indent=6)
    if auto:
        dg.fixes.append(f"清理 {len(auto)} 个自动生成的垃圾档案（名字形如「渠道用户」），"
                        f"并把它们的 identity 并回真实客户")


async def layer4_action(conn, schema, tables, dg) -> None:
    """第 4 层：核身话术是配置来的还是模型编的。"""
    print("\n[第 4 层] 核身话术的来源")
    if "crm_actions" not in tables:
        say(INFO, "crm_actions 表不存在，跳过")
        return
    tbl = f"{schema}.{qi('crm_actions')}"
    rows = (await conn.execute(text(
        f"SELECT {qi('act_key')}, {qi('action_name')}, {qi('created_at')} "
        f"FROM {tbl} WHERE {qi('created_at')} >= now() - interval '1 day' "
        f"ORDER BY {qi('created_at')} DESC LIMIT 10"))).fetchall()
    if not rows:
        say(WARN, "近 24h 没有任何动作台账")
        say(INFO, "手册里「查订单状态」是只读动作，确认✗审批✗，本就不需要核身。"
                  "没有台账 = action_execute 没被调用 = 那句核身话是模型自己编的", indent=6)
        dg.fixes.append("单独修「识别失败时模型自创核身流程」——"
                        "角色 soul.md 要求不知道就说不知道并转人工，不是发明流程")
        return
    say(OK, f"近 24h 有 {len(rows)} 条动作台账")
    for k, n, at in rows[:5]:
        say(INFO, f"{at} {n!r}", indent=6)


async def run(args) -> int:
    dsn = args.dsn or os.environ.get("CRM_DSN")
    if not dsn:
        print("缺 DSN：用 --dsn 传，或设环境变量 CRM_DSN")
        return 2

    engine = create_async_engine(dsn, pool_pre_ping=True)
    dg = Diagnosis()
    try:
        async with engine.connect() as conn:
            if args.list_schemas:
                found = await list_schemas(conn)
                print("可用的 org schema：")
                for s in found:
                    print(f"  {s}")
                if not found:
                    print("  （一个都没有，确认 DSN 连的是不是对的库）")
                return 0

            schema = args.schema or f"org_data_{str(args.org_id or '').replace('-', '')}"
            if not args.org_id and not args.schema:
                print("缺 --org-id 或 --schema。先跑 --list-schemas 看有哪些")
                return 2

            found = await list_schemas(conn)
            if schema not in found:
                print(f"schema {schema!r} 不存在。现有：{found or '（无）'}")
                return 2

            tables = await tables_in(conn, schema)
            missing = [t for t in ("crm_identities", "crm_conversations", "crm_customers")
                       if t not in tables]
            print(f"库连上了：schema={schema}，{len(tables)} 张表")
            if missing:
                print(f"必需表缺失：{missing}，先建表")
                return 2

            print("=" * 64)
            print(f"排查对象：channel={args.channel!r}  external_id={args.external_id!r}"
                  + (f"  conv_id={args.conv_id!r}" if args.conv_id else ""))
            print("=" * 64)

            cust = await layer0_identity(conn, schema, args.channel, args.external_id, dg)
            await layer1_conv(conn, schema, args.external_id, args.conv_id, cust, dg)
            await layer2_called(conn, schema, dg)
            await layer3_garbage(conn, schema, dg)
            await layer4_action(conn, schema, tables, dg)

            print("\n" + "=" * 64)
            if dg.broken_at:
                print(f"结论：断在 {dg.broken_at}")
                print(f"原因：{dg.cause}")
            else:
                print("结论：五层都没查出硬伤")
                print("原因：数据层是通的，问题更可能在角色编排——"
                      "查 service_agent 有没有按顺序调 identity_resolve")
            if dg.fixes:
                print("\n怎么修：")
                for i, f in enumerate(dg.fixes, 1):
                    print(f"  {i}. {f}")
            print("=" * 64)
        return 0
    except Exception as e:                           # noqa: BLE001 排查工具，错误要现形
        print(f"\n执行失败：{type(e).__name__}: {e}")
        print("连不上库的话，检查 DSN 格式："
              "postgresql+asyncpg://用户:密码@主机:端口/库名")
        return 1
    finally:
        await engine.dispose()


def main() -> int:
    p = argparse.ArgumentParser(
        description="身份识别链路断点定位（排查工具，不是技能）")
    p.add_argument("--dsn", help="数据库连接串，或用环境变量 CRM_DSN")
    p.add_argument("--org-id", help="org_id，用来拼 schema 名")
    p.add_argument("--schema", help="直接指定 schema，优先于 --org-id")
    p.add_argument("--channel", default="官网", help="渠道，默认 官网")
    p.add_argument("--external-id", default="", help="该渠道下的外部用户 ID")
    p.add_argument("--conv-id", default="", help="会话编号，有的话查得更准")
    p.add_argument("--list-schemas", action="store_true", help="列出所有 org schema 后退出")
    args = p.parse_args()
    if not args.list_schemas and not args.external_id:
        p.error("要么 --list-schemas，要么给 --external-id")
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
