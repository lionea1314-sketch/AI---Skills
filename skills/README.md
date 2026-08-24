# Skills · AI 客户经营系统

本目录存放《AI 客户经营系统 · 部署手册 v4》第 3 章定义的技能。

**角色 vs 技能的判断标准**：这件事需不需要"判断"？需要判断的是角色，照章办事的是技能。查订单状态不需要判断，是技能；判断这个客户现在该不该被推荐，是角色。

## 技能的标准结构

```
skills/<技能标识>/
├── SKILL.md            必需｜元数据 + 说明（决定"什么时候调我"）
├── handler.py          必需｜async def execute(input_data, runtime_context) -> dict
└── rules.default.json  可选｜默认规则，装载时 upsert 进 crm_rules
```

`skill.toml` **本仓库不使用**，出现即报错。

**rules.default.json 不手工维护**——规则的唯一事实来源是 SKILL.md 的「规则」表，
这个文件由脚本生成：

```bash
python3 scripts/gen_rules_default.py --in-place   # 生成到各技能目录
python3 scripts/gen_rules_default.py              # 只生成到 build/rules/ 预览
```

改了 SKILL.md 的规则表就重跑一次。`validate_skills.py` 会**逐字段比对两边**，
漂移了直接报错——避免出现"文档写默认 300、实际注入 500"这种查不出来的事故。

**与手册 3.1 的差异**：

| 手册 3.1 | 本仓库 | 原因 |
|---|---|---|
| `skill.toml` 可选 | **不使用** | 仓库约定 |
| `rules.default.json` 必需 | **可选，脚本生成** | 规则内容本就在 SKILL.md 的「规则」章节（手册要求那里列出全部 rule_key），生成而非手写，杜绝两份漂移 |
| 建表与读写走 nocodb 封装 | **SQLAlchemy AsyncSession + Postgres org schema** | 按《平台技能操作数据库 · 施工规范》实现，13 条铁律逐条落实 |

## 技能总表（手册 3.3）

`已建` = SKILL.md + handler.py 齐备并通过规范校验。

| # | 能力 | 标识 | 组 | 触发 | cost | 主要写表 | 规则数 | 状态 |
|---|---|---|---|---|---|---|---|---|
| S-01 | 收消息 | [`inbound_gateway`](./inbound_gateway/) | A 接待 | auto | zero | conversations / media | 4 | **已建** |
| S-02 | 认客户 | [`identity_resolve`](./identity_resolve/) | A 接待 | auto | zero | identities / customers / leads | 9 | **已建** |
| S-03 | 取客户资料 | [`read_view`](./read_view/) | A 接待 | explicit | zero | —（只读） | 6 | **已建** |
| S-04 | 认意图和情绪 | [`intent_classify`](./intent_classify/) | A 接待 | auto | low | conversations | 4 | **已建** |
| S-05 | 判断该怎么办 | [`route_decide`](./route_decide/) | A 接待 | auto | zero | — | 6 | **已建** |
| S-06 | 转人工 | [`handoff_pack`](./handoff_pack/) | A 接待 | explicit | zero | conversations / activities | 5 | **已建** |
| S-07 | 检索知识 | `kb_search` | B 应答 | explicit | low | — | 4 | 待建 |
| S-08 | 生成带出处的答案 | `kb_answer` | B 应答 | explicit | low | — | 5 | 待建 |
| S-09 | 记未答问题 | `faq_gap_collect` | B 应答 | auto | zero | faq_gaps | 3 | 待建 |
| S-10 | 出建议回复 | `suggest_reply` | B 应答 | sidecar | low | suggestions | 13 | 待建 |
| S-11 | 办事 | `action_execute` | C 办事 | explicit | zero | actions / tickets | 8+ | 待建 |
| S-12 | 跑剧本 | `procedure_run` | C 办事 | explicit | zero | actions / procedures | 3+ | 待建 |
| S-13 | 查会员 | `member_query` | C 办事 | explicit | zero | — | 3 | 待建 |
| S-14 | 积分记账 | `member_ledger` | C 办事 | cron | zero | point_ledger | 6 | 待建 |
| S-15 | 预约 | `appointment` | C 办事 | explicit | zero | slots / appointments | 12 | 待建 |
| S-16 | 发出前检查 | `guardrail_check` | D 安全 | auto | zero+low | outbound_check | 10 | 待建 |
| S-17 | 事后抽检 | `post_audit` | D 安全 | cron | low | outbound_check | 4 | 待建 |
| S-18 | 敏感信息拦截 | `sensitive_filter` | D 安全 | auto | zero+low | — | 3 | 待建 |
| S-19 | 权限校验 | `acl_check` | D 安全 | auto | zero | activities | 2 | 待建 |
| S-20 | 附件处理 | `media_process` | D 安全 | auto | medium | media | 8 | 待建 |
| S-21 | 提炼记忆 | `memory_extract` | E 分析 | cron | low | memories | 24 | 待建 |
| S-22 | 算客户指标 | `metrics_compute` | E 分析 | cron | zero | customers.custom_fields | 8 | 待建 |
| S-23 | 出提醒 | `alert_center` | E 分析 | cron | zero | alerts | 5 | 待建 |
| S-24 | 判断意向 | `intent_score` | E 分析 | auto | zero+low | intents | 9 | 待建 |
| S-25 | 出画像 | `profile_build` | E 分析 | 触发/cron | medium | profiles / profile_versions | 7 | 待建 |
| S-26 | 画像决策 | `profile_decide` | E 分析 | explicit | medium | —（出报告） | 6 | 待建 |
| S-27 | 学话术 | `script_learn` | E 分析 | cron | low | scripts | 8 | 待建 |
| S-28 | 规则效果分析 | `rule_perf` | E 分析 | cron | zero+low | rule_perf | 4 | 待建 |
| S-29 | 扮演客户 | `persona_roleplay` | F 培训 | explicit | medium | training_runs | 6 | 待建 |
| S-30 | 陪练评分 | `training_score` | F 培训 | explicit | low | training_runs / staff_skills | 5 | 待建 |
| S-31 | 场景管理 | `scenario_manage` | F 培训 | explicit | low | scenarios | 4 | 待建 |

A 组是手册部署顺序第三步（"装 ①客服接待官 + A/B 组技能 ← 系统能跑了"）里的接待闭环：
`inbound_gateway` → `identity_resolve` → `intent_classify` → `route_decide` →（分流）→ `read_view` / `handoff_pack`。

## SKILL.md 元数据模板

字段集与顺序固定，`scripts/validate_skills.py` 强制校验：

```yaml
---
name: memory_extract              # 技能标识，英文小写下划线，与目录名一致
description: 一句话说清做什么 + 什么时候调
name_zh: 提炼记忆                  # 中文名
source: builtin                   # builtin | external | learned | mcp
category: analysis                # intake|answer|action|safety|analysis|training|data
trigger-type: cron                # explicit=LLM调用 | auto=系统事件 | cron=定时 | sidecar=旁路
cost-tier: low                    # zero=不花token | low | medium | high
tags: memory, extract, customer
positive-examples:                # 供召回，写真实说法
  - "把这段会话里的客户偏好提出来"
negative-examples:                # 写"像但不是"的说法，防误召回
  - "帮我总结这次对话"            # → 那是会话摘要
domain: crm
---
```

正文固定六节 + 一节自查：**作用 / 触发方式 / 输入·输出 / 规则 / 依赖的表 / 实现 / 施工规范符合性自查**。

**cost_tier 定档标准**：`zero` = 一次模型都不调；`low` = 一次快模型；`medium` = 一次主力模型或多模态；`high` = 多轮或长上下文。**能定 zero 就不要定 low**——路由判断、意向打分、提醒生成、去重防洪、指标计算全都是 zero。

## 数据库施工规范（13 条铁律）

每个技能的 SKILL.md 末尾必须有「施工规范符合性自查」表，逐条说明落实方式。

**一、拿会话与定位表**
1. 主账号会话从 `runtime_context` 取，不自己建连接。平台注入的是 SQLAlchemy `AsyncSession`（非 asyncpg 裸连），从 `rc["db"]` 取（兼容 `session`/`db_session`）。
2. 表在 org schema，用全限定名 `org_data_<org_id去横线>."表"`，不依赖 `search_path`。org_id 取不到要报错，别默默用错 schema。

**二、写库三铁律**

3. 写完必须 `commit`。AsyncSession 不自动提交，漏了会静默回滚——诊断显示"成功"、表里空空。
4. 保留字/列名一律加双引号。`interval`/`open`/`close`/`value`/`state` 裸写必炸，统一走 `_qi(name)`。
5. 绑定参数用 `:name`，**绝不写 `:name::type`**。asyncpg 下 `::` 会解析崩，要转型用 `cast(:x AS type)`。加静态防呆 `_assert_safe_sql`。

**三、读库**

6. 先查表存在性，再决定查/跳。Postgres 对不存在的表发查询会中止整个事务，后续全部 `InFailedSQLTransactionError` 连坐。选配表缺失 = 跳过不报错；必需表缺失 = 明确报错。

**四、幂等与唯一约束**

7. 别假设唯一约束存在——平台建表器会剥 PRIMARY KEY 并自动加自增 id。一律先 SELECT 再 INSERT。
8. 两种幂等写法任选但要真幂等：有唯一约束 → `ON CONFLICT DO UPDATE`；不依赖约束 → delete-then-insert。

**五、分段隔离**

9. 每个 symbol、每类附属数据各自 try/except。附属数据（附件、台账、候选查询）绝不能阻断主数据（会话、客户主档）。单点失败后 `rollback`，避免脏事务连累后续。

**六、自测**

10. mock 的边界就是测试的盲区。自测能覆盖计算逻辑、参数校验、安全拦截；**覆盖不到、必须真库验**的是：SQL 语法、commit 是否落、约束是否补建、事务连坐。
11. 错误信息为空 = 被吞了。捕获处一律 `logger.exception`，诊断字段原样返回，不要总结成"成功"。

**七、诊断返回**

12. 返回可核验的数字，别返回"成功"。写了多少行、跳过了谁、有没有洞，`written`/`skipped`/`notes` 必须原样暴露。**"written=0 但返回 ok" 是最危险的谎言。**

**八、跨技能**

13. 技能是独立执行单元，**不能互相 import**。技能被 executor 当独立脚本跑，跨技能 import 运行时不成立。要串多个技能：用平台 `skill_schedule` 多步链，或让角色按序调。

这意味着 `_get_session` / `_qi` / `_assert_safe_sql` 这类辅助函数在每个 handler 里各写一份。**这是规范要求的取舍**：重复几十行样板，换取每个技能能被独立部署和执行。

### 上线前 checklist

db 从 rc 取 / 全限定表名 · 所有写操作 commit · 保留字加引号且无 `:name::type` · 不存在的表先查存在性 · 唯一约束自检 · 每附属数据分段 try/except + 回滚 · 返回带可核验数字 · 真库（或真 sqlite 会话）端到端验过

## 校验

```bash
python3 scripts/validate_skills.py           # 错误才失败
python3 scripts/validate_skills.py --strict  # 警告也失败
```

校验项：目录只含两个文件 · frontmatter 字段集与顺序 · name 与目录名一致 · 枚举值合法 · 正负例非空 · `async def execute(input_data, runtime_context)` 存在 · 无跨技能 import · 无 `:name::type` · 含「施工规范符合性自查」章节。

纳管范围由 [`.managed`](./.managed) 决定，不在清单里的目录标为 legacy 并跳过。

## legacy 目录

`detail-master/` 和 `amazon-kit/` 是本仓库上一轮的电商方向技能，**与本系统无关**，不遵循手册规范（无 handler.py、目录名用连字符、缺元数据字段）。它们已被 `.managed` 排除在校验之外，等待处置决定：删除，或移到独立仓库。

## 新增技能

1. `mkdir skills/<标识>`，按上面的模板写 `SKILL.md`（尤其 positive / negative-examples）
2. 定 input / output schema，缺必填参数时返回 `{"error": ...}` 而不是猜
3. 写 `handler.py`：先把纯规则能算的写完，再决定哪一步需要模型
4. 把该技能的默认规则写进 SKILL.md「规则」章节，每条四要素齐（含义/调高/调低/风险）
5. 跑 `python3 scripts/gen_rules_default.py --in-place` 生成 rules.default.json
6. 补「施工规范符合性自查」表，跑 `python3 scripts/validate_skills.py`
7. 在上面的技能总表里把状态改成「已建」
