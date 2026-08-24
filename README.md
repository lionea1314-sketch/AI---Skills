# AI 客户经营系统 · 技能库

按《AI 客户经营系统 · 部署手册 v4》实现的 Claude Code Skills。

一个在线客服系统：客户进来那一刻屏幕上就显示他是谁、买过什么、有什么忌讳、有什么坑。AI 每轮给出可用的回复建议，能自己把事办完，办不完的带着完整前因后果交给人。

**和普通 CRM 的差别**：普通 CRM 的数据靠销售手工录入，录入率长期上不去。这套系统里，**接待客户本身就是数据采集**，不需要任何人额外填表。

## 目录结构

```
.
├── skills/
│   ├── inbound_gateway/     S-01 收消息      SKILL.md + handler.py
│   ├── identity_resolve/    S-02 认客户
│   ├── read_view/           S-03 取客户资料
│   ├── intent_classify/     S-04 认意图和情绪
│   ├── route_decide/        S-05 判断该怎么办
│   ├── handoff_pack/        S-06 转人工
│   ├── .managed             纳管清单（决定谁参与规范校验）
│   └── README.md            技能总表 · 元数据模板 · 施工规范 13 条
├── scripts/
│   └── validate_skills.py   规范校验
└── README.md
```

每个技能只有两个文件：`SKILL.md`（元数据 + 说明 + 规则表 + 符合性自查）和 `handler.py`（`async def execute`）。

## 进度

**A 组·接待类 6/6 已建**，构成手册部署顺序第三步的接待闭环：

```
消息进来
   ↓
inbound_gateway   收消息、判会话归属、防重复投递      zero token
   ↓
identity_resolve  跨渠道认人，强证据才自动合并        zero token
   ↓
intent_classify   判意图与情绪，拿不准就归"其他"      low（可降级）
   ↓
route_decide      七步分流，纯规则不问模型            zero token
   ↓
 ┌─────────┬──────────┬─────────┬──────────┐
 kb      action   procedure  appointment  handoff
                                             ↓
                                      handoff_pack
                                      选人 + 交接包六件套
```

`read_view`（取客户资料九槽）在生成回复前随时被调用。

B～F 组共 25 个技能待建，清单与规格见 [`skills/README.md`](./skills/README.md)。

## 三条贯穿全系统的原则

**角色不调用角色。** 角色互相委派会让链路变长、责任不清、上下文重复传递。要串多个技能用平台 `skill_schedule` 多步链，或让角色按序调。技能之间也**不能互相 import**——技能被 executor 当独立脚本跑。

**对客户永远只有一个身份。** 客户看到的始终是"客服小安"，客户端不出现"转接"字样。内部才是 `AI处理 → 小张接手 → 小李接手`，`handled_by` 追加记录每一个碰过这个客户的人。

**返回可核验的数字，别返回"成功"。** 每个技能都返回 `written` / `skipped` / `notes`。**"written=0 但返回 ok" 是最危险的谎言。**

## 校验

```bash
python3 scripts/validate_skills.py
```

校验目录结构、frontmatter 字段集与顺序、枚举取值、`execute` 签名、跨技能 import、`:name::type` 写法、必需章节。

## 运行环境

```bash
pip install sqlalchemy[asyncio] asyncpg pyyaml
```

技能由平台 executor 调用，`runtime_context` 注入 `{org_id, role_id, staff_id, conv_id, rules, db, models}`：

- `db` — SQLAlchemy `AsyncSession`（非 asyncpg 裸连），业务表在 `org_data_<org_id去横线>` schema
- `rules` — 当前生效的规则值，来自 `crm_rules`
- `models` — 模型客户端，仅 cost-tier 非 zero 的技能会用到

## legacy

`skills/detail-master/` 与 `skills/amazon-kit/` 是上一轮的电商方向技能，与本系统无关，已排除在校验之外，等待删除或迁出。
