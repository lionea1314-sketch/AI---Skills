---
name: route_decide
description: 判断这轮该怎么办，输出 path ∈ self_answer/kb/action/procedure/appointment/handoff 和 reason。纯规则，零 token，不问模型。
name_zh: 判断该怎么办
source: builtin
category: intake
trigger-type: auto
cost-tier: zero
tags: routing, decide, handoff, rules, zero-token
positive-examples:
  - "这轮该走知识库还是转人工"
  - "客户说叫人来，怎么处理"
  - "判断一下这条消息该怎么走"
negative-examples:
  - "这条消息是什么意图"          # → intent_classify，那是分类，本技能是分流
  - "把交接包生成出来"            # → handoff_pack，本技能只决定要不要转
  - "生成一条回复"                # → suggest_reply
domain: crm
---

# route_decide

## 作用

按固定顺序判断这轮走哪条路。**纯规则，零 token，不问模型。**

路由是整条链路的岔路口，判错的代价是客户被晾着或被机器人绕圈。**它必须可预测、可复现、可解释**——所以不调模型，每个结论都带 `reason` 和命中的 `rule_key`。

## 触发方式

`auto` — `intent_classify` 之后立即调用，输出决定后续调用哪个技能。

## 输入 / 输出

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `intent_main` | string | ● | 来自 `intent_classify` |
| `emotion` | string | ○ | 正向/中性/负向 |
| `confidence` | float | ○ | 意图把握度，默认 0 |
| `risk_flags` | array | ○ | 客户卡风险位，如 `["勿扰","法务关注"]` |
| `matched_procedure` | string | ○ | 命中的剧本标识 |
| `unresolved_turns` | int | ○ | 连续未解决轮数，默认 0 |
| `customer_asked_human` | bool | ○ | 客户是否明说要人工 |
| `text` | string | ○ | 本轮原文，用于价格/人工关键词兜底识别 |
| `defined_actions` | array | ○ | 已定义动作清单，缺省用内置默认 |

| 字段 | 类型 | 说明 |
|---|---|---|
| `path` | string | `self_answer`/`kb`/`action`/`procedure`/`appointment`/`handoff` |
| `reason` | string | 人能读懂的判定理由 |
| `matched_rule` | string | 命中的 rule_key（可核验） |
| `step` | int | 命中判定顺序里的第几步（可核验） |
| `evaluated` | array | 每一步的判定过程，含未命中的（可核验，规则调参时看这个） |
| `notes` | array | 告警，如规则值非法退回默认 |

失败返回 `{"error": "..."}`，不抛异常。

## 规则

| rule_key | 规则名 | 类型 | 默认 | 范围 | 风险 | 含义 | 调高/开会怎样 | 调低/关会怎样 |
|---|---|---|---|---|---|---|---|---|
| `handoff.confidence` | 自动处理最低把握度 | 阈值 | 0.80 | 0.5~0.95 | 中 | 意图把握度低于此值直接转人工 | 转人工变多，人力成本上升，但错答变少 | AI 拿不准也硬答，客户被绕圈 |
| `handoff.max_turns` | 连续未解决轮数 | 阈值 | 3 | 1~10 | 中 | 连续几轮没解决就转人工 | 客户被机器人绕更久才见到人 | 稍有波折就转人工，人力吃紧 |
| `handoff.on_negative` | 情绪负向转人工 | 开关 | 开 | — | 中 | 判为负向立刻转人工 | 生气的客户第一时间见到人 | **关掉后投诉容易升级**，AI 继续应对愤怒客户 |
| `handoff.on_customer_ask` | 客户要人工就转 | 开关 | 开 | — | 中 | 客户明说要人工立刻转 | 尊重客户意愿，体验好 | **关掉后客户反复要人工却转不过去，是最典型的差评来源** |
| `handoff.on_price_ask` | 涉价格折扣赔付转人工 | 开关 | 开 | — | 高 | 议价、折扣、赔付话题转人工 | 避免 AI 承诺价格造成实际损失 | **关掉意味着 AI 可能对价格作出承诺，损失不可逆** |
| `route.order` | 判定顺序 | 清单 | 见下 | 可重排 | 中 | 七步判定的先后顺序 | 顺序决定谁先截胡，改了整体分流结构会变 | — |

**默认判定顺序**（`route.order`，顺序本身可改，中风险）：

```
1. 客户要人工 / 情绪负向 / 涉及价格折扣赔付        → handoff
2. 命中某剧本触发词                              → procedure
3. 意图 = 预约相关                               → appointment
4. 意图 = 查单/改址/查积分等已定义动作            → action
5. 意图 = 咨询 且 confidence ≥ handoff.confidence → kb
6. 连续未解决 ≥ handoff.max_turns                → handoff
7. 其余                                          → self_answer
```

第 1 步永远建议留在最前：**客户说"叫人来"却还要先被 AI 答一轮，是最典型的差评来源。**

## 依赖的表

**读**：无　**写**：无

本技能是纯函数，不碰数据库。风险位由调用方从 `read_view` 的第②槽带进来，剧本命中由 `procedure_run` 的触发词表带进来——跨技能 import 不成立，靠角色按序调用传参。

## 实现

见同目录 `handler.py`。

## 施工规范符合性自查

| # | 规范要点 | 本技能落实 |
|---|---|---|
| 1 | db 从 runtime_context 取 | 不需要 db；纯函数不建连接，也不碰 rc["db"] |
| 2 | org 全限定表名 | 不涉及（无表访问） |
| 3 | 写完必须 commit=True | 不涉及（无写操作） |
| 4 | 保留字/列名加双引号 | 不涉及（无 SQL） |
| 5 | 绝不写 :name::type | 不涉及（无 SQL） |
| 6 | 不存在的表先查存在性 | 不涉及（无表访问） |
| 7 | 不假设唯一约束存在 | 不涉及 |
| 8 | 真幂等写法 | 纯函数，同输入必同输出，天然幂等 |
| 9 | 分段 try/except | 每步判定独立求值，单步异常记入 `evaluated` 后继续，不中断整链 |
| 10 | 不只 mock，真库验 | 纯逻辑技能，离线用例可完整覆盖，**无需真库验** |
| 11 | 错误现形，不吞 traceback | 规则值非法时 `logger.warning` 并写入 `notes`，不静默吞掉 |
| 12 | 返回可核验数字 | `matched_rule`/`step`/`evaluated` 完整暴露判定过程，规则调参直接看它 |
| 13 | 不跨技能 import | 无跨技能 import；风险位与剧本命中由角色按序调用传参 |
