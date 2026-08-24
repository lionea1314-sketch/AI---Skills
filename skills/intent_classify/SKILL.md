---
name: intent_classify
description: 读最后一轮客户消息并结合上下文，判断主意图、子意图和情绪，输出 intent_main / sub_intent / emotion / confidence。只分类，不回答问题。
name_zh: 认意图和情绪
source: builtin
category: intake
trigger-type: auto
cost-tier: low
tags: intent, emotion, classify, routing, nlu
positive-examples:
  - "这条消息客户是什么意思"
  - "客户是不是生气了"
  - "判断一下这轮的意图"
negative-examples:
  - "回复一下这个客户"            # → suggest_reply / kb_answer
  - "这个客户该不该转人工"        # → route_decide
  - "把这次会话总结一下"          # → 会话摘要，不是意图分类
domain: crm
---

# intent_classify

## 作用

读最后一轮客户消息，结合前 N 轮上下文，输出主意图、子意图、情绪、把握度。**只做分类，不回答问题。**

分类结果是 `route_decide` 的输入，它决定这轮走知识库、走动作、还是直接转人工。**判错的代价是路由错**，所以宁可给低 `confidence` 选"其他"，也不要硬选。

## 触发方式

`auto` — `identity_resolve` 之后、`route_decide` 之前，每轮客户消息触发一次。

## 输入 / 输出

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string | ● | 本轮客户消息 |
| `conv_id` | string | ○ | 写回分类结果用；缺省则只返回不落库 |
| `context` | array | ○ | 前 N 轮 `[{role, text}]`，缺省按 `intent.context_turns` 从库里取 |
| `cust_id` | string | ○ | 仅用于日志归因 |

| 字段 | 类型 | 说明 |
|---|---|---|
| `intent_main` | string | 咨询/售后/投诉/购买/会员/预约/其他（清单可增删） |
| `sub_intent` | string | 自由文本子意图 |
| `emotion` | string | 正向/中性/负向 |
| `confidence` | float | 0~1，低于 `intent.min_confidence` 时 `intent_main` 强制为"其他" |
| `low_confidence` | bool | 是否低于门槛 |
| `degraded` | bool | 模型不可用时降级为关键词规则，此项为 `true` |
| `model_used` | string\|null | 实际调用的模型标识（可核验） |
| `written` | int | 写回 `crm_conversations` 的行数（可核验） |
| `skipped` / `notes` | array | 跳过项与错误 |

失败返回 `{"error": "..."}`，不抛异常。

## 规则

| rule_key | 规则名 | 类型 | 默认 | 范围 | 风险 | 含义 | 调高会怎样 | 调低会怎样 |
|---|---|---|---|---|---|---|---|---|
| `intent.class_list` | 意图清单 | 清单 | 咨询/售后/投诉/购买/会员/预约/其他 | 可增删 | 中 | 主意图只能从这个清单里选 | 分得细，路由规则要跟着补，漏配就掉进"其他" | 分得粗，不同问题走同一条路由 |
| `intent.context_turns` | 带几轮上下文 | 阈值 | 3 | 0~10 | 低 | 判断时带多少轮历史 | 判断更准，token 成本上升 | 指代不清的短句判不准（"那个呢"） |
| `intent.min_confidence` | 最低把握度 | 阈值 | 0.5 | 0.3~0.9 | 中 | 低于此值强制归为"其他" | "其他"变多，更多走人工兜底 | 低把握结果被当真，路由错得更多 |
| `intent.emotion_negative_sensitivity` | 负向情绪灵敏度 | 单选 | 中 | 低/中/高 | 中 | 判负向的宽严 | 更容易判负向，转人工变多、成本上升 | 真生气的客户被判中性，投诉升级 |

**提示词骨架**（可编辑、有版本，走 `crm_rule_versions` 管理；平台在 `rules["intent.prompt_template"]` 里配了就用配的，没配用内置默认）：

```
你只做分类，不回答问题。读最后一轮客户消息，结合上下文，输出 JSON：
{"intent_main":"", "sub_intent":"", "emotion":"", "confidence":0.0}
intent_main 只能从清单里选（清单可在界面增删）：{class_list}
emotion 只能是：正向/中性/负向
准则：
· 只看客户说的，不看我方说的
· 一句话里有多个意图，选最需要立刻处理的那个
· 反问句、语气词、标点密度参与情绪判断，但不要过度解读
· 不确定就给低 confidence 并选"其他"，不要硬选
```

## 依赖的表

**读**：`crm_conversations`（取上下文轮次与当前状态）
**写**：`crm_conversations.intent_main` / `.emotion`（仅当传入 `conv_id`）

## 实现

见同目录 `handler.py`。

**模型契约**：`runtime_context["models"]` 需提供以下任一形态，handler 逐个尝试：
`models.complete(prompt=..., tier=...)` / `models.chat(messages=[...], tier=...)` / `models(prompt)`（可调用对象）。返回值取 `text` / `content` / `output` 字段或直接字符串。

**降级路径**：模型缺失、超时或返回非 JSON 时，退回内置关键词规则给出粗分类，`degraded=true` 且 `confidence` 封顶 0.45（低于默认门槛，必然落进"其他"走人工兜底）。**宁可降级也不让整条接待链路断在这里。**

## 施工规范符合性自查

| # | 规范要点 | 本技能落实 |
|---|---|---|
| 1 | db 从 runtime_context 取 | `_get_session`，兼容 `db`/`session`/`db_session`；无 db 时仍能只分类不落库 |
| 2 | org 全限定表名 | `_org_schema` + `_tbl`；需落库而 org_id 缺失时报错 |
| 3 | 写完必须 commit=True | `_write_back` 写后 `await session.commit()`，并返回 `written` 行数 |
| 4 | 保留字/列名加双引号 | 全部列名走 `_qi`，`text`/`state` 均加引号 |
| 5 | 绝不写 :name::type | `_assert_safe_sql` 每条 SQL 执行前静态防呆 |
| 6 | 不存在的表先查存在性 | `_existing_tables`；`crm_conversations` 缺则跳过写回并记 `skipped`，不报错 |
| 7 | 不假设唯一约束存在 | 只 UPDATE 已有会话行，不做 upsert |
| 8 | 真幂等写法 | 同一轮重复调用得到同一分类并覆盖写回，不产生新行 |
| 9 | 分段 try/except | 取上下文、调模型、写回三段各自 try + rollback，互不连坐 |
| 10 | 不只 mock，真库验 | 降级规则、JSON 解析、门槛逻辑离线可验；SQL 写回与 commit **需真库验** |
| 11 | 错误现形，不吞 traceback | 捕获处 `logger.exception`，模型原始返回截断后进 `notes` |
| 12 | 返回可核验数字 | `confidence`/`written`/`degraded`/`model_used`/`skipped`/`notes` |
| 13 | 不跨技能 import | 无跨技能 import；下游 `route_decide` 由角色按序调用 |
