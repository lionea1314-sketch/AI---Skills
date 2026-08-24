---
name: identity_resolve
description: 拿渠道 ID 找到人；找不到按规则建线索或客户；合并只在强证据下自动做，弱证据一律只标待确认。返回 cust_id / lead_id / matched_by / confidence / merge_state。
name_zh: 认客户
source: builtin
category: intake
trigger-type: auto
cost-tier: zero
tags: identity, merge, customer, lead, cross-channel
positive-examples:
  - "这个抖音 ID 是我们哪个客户"
  - "同一个手机号在微信和官网，是不是一个人"
  - "客户说他微信是 xxx，帮他关联上"
negative-examples:
  - "把这个客户的资料拉出来"      # → read_view
  - "这条消息归到哪个会话"        # → inbound_gateway
  - "这两个客户是什么关系"        # → 关系走 crm_relations，不是身份合并
domain: crm
---

# identity_resolve

## 作用

拿渠道 ID 找到人；找不到按规则建线索或客户；**合并只在强证据下自动做**。纯规则，零 token。

**合并错了比不合并严重得多**：把两个人错认成一个，等于把 A 的订单和聊天记录暴露在 B 的服务界面上。所以弱证据只能标待确认，且每次合并都必须可撤销。

## 触发方式

`auto` — `inbound_gateway` 落下会话后立即调用，把 conv 挂到人身上。下游 `read_view` / `intent_classify` 依赖它的 `cust_id`。

## 输入 / 输出

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `channel` | string | ● | 渠道 |
| `external_id` | string | ● | 该渠道下的外部 ID |
| `phone` | string | ○ | 强证据 |
| `unionid` | string | ○ | 强证据 |
| `self_claim` | object | ○ | 客户自述，`{channel, external_id}`，强证据 |
| `email` | string | ○ | 强证据但默认关（共用邮箱风险） |
| `addr_name` | object | ○ | `{address, name}`，强证据但默认关 |
| `weak_signals` | object | ○ | `{display_name, region, industry, avatar_hash}`，弱证据 |
| `name` | string | ○ | 新建客户时的称呼 |
| `conv_id` | string | ○ | 回填会话归属用 |

| 字段 | 类型 | 说明 |
|---|---|---|
| `cust_id` | string\|null | 命中或新建的客户编号 |
| `lead_id` | string\|null | `identity.new_as_lead` 开启时新建的线索编号 |
| `matched_by` | string | `identity_key` / `phone` / `unionid` / `self_claim` / `email` / `addr_name` / `weak` / `new` |
| `confidence` | float | 0~1 |
| `merge_state` | string | 已确认 / 待确认 / 已否决 |
| `is_new` | bool | 是否新建了客户或线索 |
| `merge_evidence_key` | string\|null | 合并快照标识，撤销靠它 |
| `candidates` | array | 弱证据命中的候选，人工确认用（可核验） |
| `skipped` / `notes` | array | 跳过项与错误，原样透出 |

失败返回 `{"error": "..."}`，不抛异常。

## 规则

| rule_key | 规则名 | 类型 | 默认 | 风险 | 含义 | 调高/开会怎样 | 调低/关会怎样 |
|---|---|---|---|---|---|---|---|
| `identity.merge_by_phone` | 手机号自动合并 | 开关 | 开 | 中 | 手机号一致直接合并 | 跨渠道认人准，历史攒得住 | 同一人多份档案，画像被稀释 |
| `identity.merge_by_unionid` | unionid 自动合并 | 开关 | 开 | 中 | 平台 unionid 打通 | 微信生态内认人准 | 公众号/小程序各算一个人 |
| `identity.merge_by_self_claim` | 自述自动合并 | 开关 | 开 | 中 | 客户自己说"我微信是X" | 省人工确认 | 自述要人工核，响应变慢 |
| `identity.merge_by_email` | 邮箱自动合并 | 开关 | **关** | 高 | 邮箱一致直接合并 | 认人多一条路；**共用邮箱会把两个人并成一个** | 邮箱只做候选提示 |
| `identity.merge_by_addr_name` | 地址+姓名自动合并 | 开关 | **关** | 高 | 地址与姓名同时一致 | 电商场景认人准；**同名同住址的家人会被并** | 只做候选提示 |
| `identity.weak_to_pending` | 弱证据只标待确认 | 开关 | 开 | 高 | 昵称像/头像同/同地区行业只挂待确认 | 安全，错并率低 | **关掉等于允许弱证据自动合并，强烈不建议** |
| `identity.merge_reversible` | 合并保留快照可撤销 | 开关 | 开 | 高 | 合并前写双方主档快照 | 误并可一键回退 | **关掉后合并不可逆，出错只能人工重建** |
| `identity.pending_remind_days` | 待确认提醒天数 | 阈值 | 3 | 低 | 待确认多久没处理就提醒 | 队列积压久，客户体验受影响 | 提醒频繁，打扰坐席 |
| `identity.new_as_lead` | 新身份先建线索 | 开关 | 关 | 中 | 新身份建 leads 而不是 customers | 客户主档干净，问一句就走的不进档 | 主档里混入大量一次性访客 |

## 依赖的表

**读**：`crm_identities`、`crm_customers`、`crm_leads`（可选）
**写**：`crm_identities`、`crm_customers`（新建）、`crm_leads`（可选）、`crm_conversations.cust_id`（回填，可选）

`crm_leads` 不存在时 `identity.new_as_lead` 自动降级为建 customers，并在 `notes` 里说明——选配表缺失是跳过，不是报错。

## 实现

见同目录 `handler.py`。

**匹配优先级**（顺序固定，不可配，因为它直接决定错并风险）：
`identity_key`（99% 走这条）→ `phone` → `unionid` → `self_claim` → `email`(默认关) → `addr_name`(默认关) → 弱证据(只标待确认) → 新建。

命中即返回，不继续往下试，避免一条弱证据覆盖强证据的结论。

## 施工规范符合性自查

| # | 规范要点 | 本技能落实 |
|---|---|---|
| 1 | db 从 runtime_context 取 | `_get_session`，兼容 `db`/`session`/`db_session` |
| 2 | org 全限定表名 | `_org_schema` + `_tbl`；org_id 取不到直接报错 |
| 3 | 写完必须 commit=True | `_insert_identity` / `_create_customer` / `_create_lead` / `_backfill_conv` 写后均 commit |
| 4 | 保留字/列名加双引号 | 全部列名走 `_qi`，`state`/`name`/`email` 均加引号 |
| 5 | 绝不写 :name::type | `_assert_safe_sql` 每条 SQL 执行前静态防呆 |
| 6 | 不存在的表先查存在性 | `_existing_tables`；`crm_identities`/`crm_customers` 缺则报错，`crm_leads`/`crm_conversations` 缺则跳过 |
| 7 | 不假设唯一约束存在 | `identity_key`/`cust_id` 全部先 SELECT 再 INSERT，不用 ON CONFLICT；编号冲突重试 3 次 |
| 8 | 真幂等写法 | 同一 `channel:external_id` 重复调用命中步骤 1，直接返回同一 cust_id，零新增 |
| 9 | 每附属数据分段 try/except | 会话回填、弱证据候选各自 try + rollback，不连坐主匹配 |
| 10 | 不只 mock，真库验 | 匹配优先级与证据判定离线可验；SQL 语法、commit 落盘、编号并发冲突**需真库验** |
| 11 | 错误现形，不吞 traceback | 捕获处 `logger.exception`，错误原样进 `notes` |
| 12 | 返回可核验数字 | `matched_by`/`confidence`/`candidates`/`is_new`/`skipped`/`notes` |
| 13 | 不跨技能 import | 无跨技能 import；权限校验交由平台在写操作前串 `acl_check` |
