---
name: handoff_pack
description: 转人工：按分派规则选出接手人，生成交接包六件套，改写会话归属并留痕。客户侧不发任何提示，不出现"转接"字样。
name_zh: 转人工
source: builtin
category: intake
trigger-type: explicit
cost-tier: zero
tags: handoff, assign, packet, staff, continuity
positive-examples:
  - "把这个会话转给人工"
  - "客户要找主管，安排一下"
  - "生成交接包给接手的同事"
negative-examples:
  - "这轮该不该转人工"            # → route_decide，那是判断，本技能是执行
  - "这个客户的资料给我"          # → read_view
  - "记一下这次转人工的原因"      # 本技能已自动写 handoff_reason，无需单独调
domain: crm
---

# handoff_pack

## 作用

选人 → 生成交接包 → 改写会话归属 → 留痕。**纯规则，零 token。**

**对客户永远只有一个身份。** 客户看到的始终是"客服小安"，客户端不出现"转接"字样，也不发任何提示——由 `handoff.no_transfer_words` 词表在 `guardrail_check` 出站口拦截。内部视角才是 `AI处理 → 小张接手 → 小李接手`。

**交接包六件套**（`handoff.packet_items` 可增删）：客户全景（含画像）／本次会话摘要／AI 试过什么·为什么没成／建议下一步／完整历史消息／风险条。

## 触发方式

`explicit` — `route_decide` 判出 `path=handoff` 后由接待官调用，或坐席手动发起。

## 输入 / 输出

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `conv_id` | string | ● | 要转的会话 |
| `reason` | string | ● | 转人工原因，写进 `handoff_reason` |
| `cust_id` | string | ○ | 缺省从会话行读 |
| `intent_main` | string | ○ | 决定按 `type_map` 分到哪个组 |
| `customer_view` | object | ○ | `read_view` 的产物；不传则本技能做一次精简查询 |
| `ai_attempts` | array | ○ | AI 试过什么·为什么没成，`[{tried, result}]` |
| `next_steps` | array | ○ | 建议下一步 |
| `target_staff_id` | string | ○ | 指定接手人，跳过自动分派 |

| 字段 | 类型 | 说明 |
|---|---|---|
| `assignee` | object\|null | 接手人 `{staff_id, name, dept, current_load}` |
| `packet` | object | 交接包，按 `packet_items` 组装 |
| `assign_mode_used` | string | 实际用的分派方式（可核验） |
| `candidates_considered` | int | 参与分派的候选人数（可核验） |
| `written` | object | `{conversations: n, activities: n}`（可核验） |
| `customer_notified` | bool | 恒为 `false`——客户侧不发任何提示 |
| `skipped` / `notes` | array | 跳过项与错误 |

无人可分派时返回 `assignee=null` 且 `packet` 照常生成，`notes` 说明原因——**交接包生成不了比没人接更糟**，至少要让会话进入待认领状态。

失败返回 `{"error": "..."}`，不抛异常。

## 规则

| rule_key | 规则名 | 类型 | 默认 | 风险 | 含义 | 调整影响 |
|---|---|---|---|---|---|---|
| `handoff.assign_mode` | 分派方式 | 单选 | 按问题类型 | 中 | 按问题类型／按归属员工／轮询／负载最低 | 改成轮询会打断客户与固定对接人的连续性 |
| `handoff.type_map` | 类型→组映射 | 清单 | 售前咨询→售前组；售后退换投诉→售后组；会员积分→会员组；预约→前台 | 中 | 意图分到哪个组 | 漏配的意图会落到默认组，容易积压 |
| `handoff.prefer_owner` | 优先给归属员工 | 开关 | 关 | 低 | 有归属员工时优先给他 | 开启后大客户体验连续，但归属员工可能过载 |
| `handoff.packet_items` | 交接包内容 | 清单 | 六件套全开 | 低 | 交接包带哪几项 | 砍掉「AI 试过什么」会让接手人重复问客户同样的问题 |
| `handoff.no_transfer_words` | 客户侧禁用词 | 清单 | 转接/换人/售后组/转给/工单/升级处理 | 中 | 这些词不得出现在客户侧 | 词表漏了会破坏"只有一个身份"的体验 |

`handoff.no_transfer_words` 由本技能返回给 `guardrail_check` 在出站口执行，本技能自身不发送任何对客消息。

## 依赖的表

**读**：`crm_conversations`、`crm_staff`；可选 `crm_customers`、`crm_memories`、`crm_customer_tags`
**写**：`crm_conversations`（`handled_by` 追加、`current_assignee`、`handoff_reason`、`state`）、`crm_activities`（可选，`act_type=系统事件`）

`handled_by` 是**追加**不是覆盖：结果形如 `AI,S001,S007`，接手过的人一个都不能丢——它是"谁碰过这个客户"的唯一记录。

## 实现

见同目录 `handler.py`。

**客户全景的来源**：优先用入参 `customer_view`（角色按序调 `read_view` 的产物）。跨技能 import 不成立，所以不传时本技能自己做一次精简查询（主档 + 钉选记忆 + 风险标签），并在 `notes` 里注明来源是精简版而非完整九槽。

## 施工规范符合性自查

| # | 规范要点 | 本技能落实 |
|---|---|---|
| 1 | db 从 runtime_context 取 | `_get_session`，兼容 `db`/`session`/`db_session` |
| 2 | org 全限定表名 | `_org_schema` + `_tbl`；org_id 取不到直接报错 |
| 3 | 写完必须 commit=True | `_assign_conv` / `_log_activity` 写后均 commit，并返回 `written` 行数 |
| 4 | 保留字/列名加双引号 | 全部列名走 `_qi`，`state`/`name`/`role` 均加引号 |
| 5 | 绝不写 :name::type | `_assert_safe_sql` 每条 SQL 执行前静态防呆 |
| 6 | 不存在的表先查存在性 | `_existing_tables`；`crm_conversations`/`crm_staff` 缺则报错，`crm_activities` 等缺则跳过 |
| 7 | 不假设唯一约束存在 | 只 UPDATE 已有会话行，`crm_activities` 为 append-only，不做 upsert |
| 8 | 真幂等写法 | 重复转给同一人时 `handled_by` 不重复追加，`written` 如实反映实际改动行数 |
| 9 | 分段 try/except | 选人、组包、写会话、写台账各自 try + rollback；台账失败不回滚会话归属 |
| 10 | 不只 mock，真库验 | 分派逻辑与 `handled_by` 追加规则离线可验；SQL、commit、并发抢人**需真库验** |
| 11 | 错误现形，不吞 traceback | 捕获处 `logger.exception`，错误原样进 `notes` |
| 12 | 返回可核验数字 | `written`/`candidates_considered`/`assign_mode_used`，不返回空泛"成功" |
| 13 | 不跨技能 import | 无跨技能 import；客户全景靠入参传入，禁用词表返回给 `guardrail_check` 执行 |
