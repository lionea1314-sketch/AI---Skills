---
name: inbound_gateway
description: 各渠道消息统一收进来，判断归属哪一段会话，防重复投递。返回 conv_id / is_new_conv / is_duplicate / media_keys。
name_zh: 收消息
source: builtin
category: intake
trigger-type: auto
cost-tier: zero
tags: inbound, gateway, conversation, dedup, channel
positive-examples:
  - "微信来了条新消息，收一下"
  - "抖音这条消息归到哪个会话"
  - "这条消息是不是重复投递的"
negative-examples:
  - "这条消息是什么意图"          # → intent_classify
  - "这个人是谁"                  # → identity_resolve
  - "把这段会话总结一下"          # → memory_extract
domain: crm
---

# inbound_gateway

## 作用

各渠道消息统一收进来，判断归属哪一段会话，防重复投递。**纯规则，零 token。**

## 触发方式

`auto` — 平台收到任意渠道的入站消息时第一个调用，是整条接待链路的入口。上游没有别的技能，下游按序是 `identity_resolve` → `intent_classify` → `route_decide`。

## 输入 / 输出

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `channel` | string | ● | 官网/微信/企微/公众号/小程序/抖音/小红书/淘宝/邮件/电话/API |
| `external_id` | string | ● | 该渠道下的客户外部 ID |
| `platform_msg_id` | string | ● | 平台消息 ID，去重靠它 |
| `text` | string | ○ | 消息正文，超长按 `gateway.max_msg_len` 截断 |
| `media` | array | ○ | `[{media_type, file_name, file_size, duration_s, storage_url, thumbnail_url}]` |
| `ts` | string | ○ | ISO8601 时间戳，缺省取当前 UTC |
| `cust_id` | string | ○ | 已知客户则带上；不带则留空由 `identity_resolve` 回填 |

| 字段 | 类型 | 说明 |
|---|---|---|
| `conv_id` | string | 归属会话编号 |
| `is_new_conv` | bool | 是否新开了会话 |
| `is_duplicate` | bool | 重复投递，为 true 时不做任何写入 |
| `media_keys` | array | 落库成功的附件键，交给下游 `media_process` |
| `turn_count` | int | 该会话当前轮数（可核验） |
| `text_truncated` | bool | 正文是否被截断 |
| `skipped` | array | 被跳过的附属数据及原因 |
| `notes` | array | 错误与告警原样透出，不吞 |

失败返回 `{"error": "..."}`，不抛异常。

## 规则

装载时逐条 upsert 进 `crm_rules`（**已存在的不覆盖**，保留企业改过的值），按 `owner_skill=inbound_gateway` 分组显示。

| rule_key | 规则名 | 类型 | 默认 | 范围 | 风险 | 含义 | 调高会怎样 | 调低会怎样 |
|---|---|---|---|---|---|---|---|---|
| `gateway.channels_enabled` | 启用渠道 | 清单 | 全开 | 渠道枚举子集 | 低 | 只收清单内渠道的消息 | 收得多，噪音和成本上升 | 关掉的渠道消息直接丢，客户以为没人理 |
| `gateway.dedup_window_s` | 去重窗口(秒) | 阈值 | 300 | 60~1800 | 低 | 多少秒内同 `platform_msg_id` 算重复 | 重复投递拦得更干净；跨窗口的正常重发会被误杀 | 平台重试时会重复计轮，turn_count 虚高 |
| `gateway.new_conv_gap_min` | 新会话间隔(分钟) | 阈值 | 30 | 5~240 | 低 | 距上条消息多久算新会话 | 会话变长，一次服务过程被拉成流水账 | 会话被切碎，画像分析的单位失真 |
| `gateway.max_msg_len` | 单条正文上限 | 阈值 | 4000 | 500~20000 | 低 | 超长正文截断长度 | 长文完整保留，下游 token 成本上升 | 长文被砍，语义丢失 |

## 依赖的表

**读**：`crm_identities`、`crm_conversations`
**写**：`crm_conversations`（新建或更新 turn_count/last 活跃）、`crm_media`（附属，失败不阻断主流程）

去重键的落点：手册未给入站消息流水表（消息正文在平台侧）。本技能把最近处理过的 `platform_msg_id` 连同时间戳维护在 `crm_conversations.custom_fields.recent_msg_ids` 滑动窗口里。去重窗口 300 秒远小于新会话间隔 30 分钟，重投必然落在同一会话内，因此该窗口足够，且不额外建表。若后续入站量大到需要独立索引，再升级为独立去重表。

## 实现

见同目录 `handler.py`。

## 施工规范符合性自查

| # | 规范要点 | 本技能落实 |
|---|---|---|
| 1 | db 从 runtime_context 取 | `_get_session`，兼容 `db`/`session`/`db_session` |
| 2 | org 全限定表名，不依赖 search_path | `_org_schema` + `_tbl` → `org_data_xxx."crm_conversations"`；org_id 取不到直接报错 |
| 3 | 写完必须 commit=True | `_upsert_conv` / `_insert_media` 写后 `await session.commit()` |
| 4 | 保留字/列名加双引号 | 所有列名走 `_qi`，`state`/`text`/`channel` 均加引号 |
| 5 | 绝不写 :name::type | `_assert_safe_sql` 静态防呆，每条 SQL 执行前过一遍 |
| 6 | 不存在的表先查存在性 | `_existing_tables` 一次性查 schema；`crm_conversations` 缺则报错，`crm_media`/`crm_identities` 缺则跳过不报错 |
| 7 | 不假设唯一约束存在 | 会话与附件均先 SELECT 再 INSERT，不用 ON CONFLICT |
| 8 | 真幂等写法 | `platform_msg_id` 命中窗口即返回 `is_duplicate=true` 且零写入，可重复触发 |
| 9 | 每附属数据分段 try/except | 每个 media 各自 try + `rollback`，附件失败不连坐会话主写入 |
| 10 | 不只 mock，真库验 | 会话归属/截断/去重逻辑离线可验；SQL 语法、commit 落盘、事务连坐**需真库验** |
| 11 | 错误现形，不吞 traceback | 捕获处 `logger.exception`，错误原样进 `notes` |
| 12 | 返回可核验数字 | `turn_count`/`media_keys`/`skipped`/`notes`，不返回空泛"成功" |
| 13 | 不跨技能 import | 无跨技能 import；附件交给 `media_process` 靠返回 `media_keys` + 平台 skill_schedule 串联 |
