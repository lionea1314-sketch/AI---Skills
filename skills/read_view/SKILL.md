---
name: read_view
description: 一次拉齐客户卡九个槽位（画像/风险条/会员卡/最近三单/偏好禁忌/未闭环/下次预约/关系/指标），按字段定义做脱敏和 AI 可见性过滤。只读，零 token。
name_zh: 取客户资料
source: builtin
category: intake
trigger-type: explicit
cost-tier: zero
tags: customer, view, profile, masking, acl, readonly
positive-examples:
  - "把这个客户的资料拉出来"
  - "客户卡上都有什么"
  - "这个客户有什么坑要注意"
negative-examples:
  - "这个抖音 ID 是谁"            # → identity_resolve
  - "给这个客户出个画像"          # → profile_build，那是生成不是读取
  - "这个客户记了什么偏好"        # → 本技能第⑤槽，但若要"提炼新记忆"则是 memory_extract
domain: crm
---

# read_view

## 作用

一次拉齐客户卡九个槽位，按 `crm_field_defs` 做脱敏和 AI 可见性过滤。**只读，零 token，不写任何表。**

九槽固定顺序：①一句话画像 ②**风险条** ③会员卡 ④最近三单 ⑤偏好与禁忌 ⑥未闭环 ⑦下次预约 ⑧关系 ⑨指标

**顺序别乱改**：坐席三秒内只看最上两块，"有什么坑"（投诉、法务关注、勿扰、特批价）必须在第二位，因为它决定这句话能不能说。风险条不受 `view.section_char_cap` 截断——被截断的风险提示等于没有。

**每一项可点开看来源**：每个槽位都带 `source` 字段。不可溯源的信息坐席不会信也不敢用。

## 触发方式

`explicit` — 接待官在生成回复前调用，或坐席打开客户卡时调用。`for_ai=true` 给模型，`for_ai=false` 给坐席界面，两者过滤规则完全不同。

## 输入 / 输出

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cust_id` | string | ● | 客户编号 |
| `sections` | array | ○ | 只取指定槽位，缺省九槽全取 |
| `for_ai` | bool | ○ | 默认 `false`。`true` 时执行 AI 可见性过滤 |

| 字段 | 类型 | 说明 |
|---|---|---|
| `sections` | object | 九槽内容，每槽带 `items` / `source` / `truncated` |
| `partial` | bool | 有槽位失败或超时则为 `true` |
| `failed_sections` | array | 哪几槽没取到及原因（可核验） |
| `masked_fields` | array | 被掩码的字段名 |
| `hidden_fields` | array | `for_ai=true` 时被整体拿掉的字段名 |
| `elapsed_ms` | int | 实际耗时 |
| `skipped` / `notes` | array | 跳过项与错误 |

失败返回 `{"error": "..."}`，不抛异常。**单槽失败绝不整体失败**——客户卡少一块比打不开强。

## 规则

| rule_key | 规则名 | 类型 | 默认 | 范围 | 风险 | 含义 | 调高会怎样 | 调低会怎样 |
|---|---|---|---|---|---|---|---|---|
| `view.section_char_cap` | 每槽字数上限 | 阈值 | 300 | 100~1000 | 低 | 单槽超长截断（风险条豁免） | 信息全，AI 上下文成本上升 | 关键细节被砍 |
| `view.orders_recent_n` | 带几单 | 阈值 | 3 | 1~10 | 低 | 最近订单条数 | 历史全，噪音多 | 看不出复购规律 |
| `view.memories_top_n` | 带几条记忆 | 阈值 | 8 | 3~20 | 低 | 偏好禁忌条数 | 上下文变长 | 重要禁忌可能落选 |
| `view.memories_rank` | 记忆排序 | 文字准则 | `hit_count×confidence` 降序，钉选置顶 | — | 低 | 记忆排序口径 | — | 改错会让钉选记忆掉出前 N |
| `view.cache_ttl_s` | 缓存秒数 | 阈值 | 60 | 0~600 | 低 | 同客户重复取的缓存时间 | 更快更省，但改了资料不立刻生效 | 每次都查库，延迟上升 |
| `view.expose_sensitive` | 敏感字段进 AI 上下文 | 开关 | **关** | — | **极高** | 手机号等敏感字段是否给模型看 | **开启后敏感信息会进入模型上下文与日志，泄露不可逆** | 模型只拿到掩码值 |

`view.expose_sensitive` 是六条极高风险规则之一，**建议保持默认关**。改它需要 owner 填原因 + 二次确认 + 明确知悉后果 + 全员通知 + 永久留档。

## 依赖的表

**读**：`crm_customers`、`crm_field_defs`、`crm_customer_tags`、`crm_memories`、`crm_relations`；可选 `crm_profiles`、`crm_members`、`crm_mock_orders`、`crm_tickets`、`crm_appointments`、`crm_conversations`
**写**：无

选配表缺失 = 该槽留空并进 `skipped`，不报错。必需表 `crm_customers` 缺失 = 明确报错。

## 实现

见同目录 `handler.py`。

**两条过滤链**：

- `for_ai=true`：逐字段查 `crm_field_defs.expose_to_ai`，false 的**整个不放进结果**；`is_sensitive` 的只给掩码；成本、底价、内部备注一律不出现。
- `for_ai=false`（给坐席看）：按 `acl_check` 渲染。本技能不自行判权限——权限校验是 `acl_check` 的职责，跨技能 import 不成立，由平台在调用前串 `acl_check` 或由角色按序调用。

**关于"并发取九槽"**：手册要求并发。平台注入的是单个 SQLAlchemy `AsyncSession`，同一 session 并发执行会抛 `InvalidRequestError`，因此本实现改为**顺序取 + 单槽超时预算 + 单槽独立 try/except**，对外语义（超时留空、`partial=true`）与手册一致。若平台后续注入 session factory，可在 `_gather_sections` 一处改为 `asyncio.gather` 而不动其它代码。

## 施工规范符合性自查

| # | 规范要点 | 本技能落实 |
|---|---|---|
| 1 | db 从 runtime_context 取 | `_get_session`，兼容 `db`/`session`/`db_session` |
| 2 | org 全限定表名 | `_org_schema` + `_tbl`；org_id 取不到直接报错 |
| 3 | 写完必须 commit=True | 本技能只读，不写库，无 commit 需求 |
| 4 | 保留字/列名加双引号 | 全部列名走 `_qi`，`state`/`name`/`value` 均加引号 |
| 5 | 绝不写 :name::type | `_assert_safe_sql` 每条 SQL 执行前静态防呆 |
| 6 | 不存在的表先查存在性 | `_existing_tables`；`crm_customers` 缺则报错，其余槽位表缺则跳过 |
| 7 | 不假设唯一约束存在 | 只读技能，不做 upsert，不依赖任何约束 |
| 8 | 真幂等写法 | 纯读，天然幂等，可无限次重复调用 |
| 9 | 每槽分段 try/except | 九槽各自 try + `rollback`，一槽失败不连坐其余八槽 |
| 10 | 不只 mock，真库验 | 掩码/过滤/排序逻辑离线可验；SQL 语法、事务连坐**需真库验** |
| 11 | 错误现形，不吞 traceback | 捕获处 `logger.exception`，失败槽位与原因原样进 `failed_sections` |
| 12 | 返回可核验数字 | `failed_sections`/`masked_fields`/`hidden_fields`/`elapsed_ms`/`partial` |
| 13 | 不跨技能 import | 无跨技能 import；坐席侧权限由平台串 `acl_check` |
