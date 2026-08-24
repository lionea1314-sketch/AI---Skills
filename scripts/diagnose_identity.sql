-- 身份识别链路断点定位（手工版）
--
-- 这不是技能，智能体不调用它。它是给人在数据库客户端里跑的排查 SQL。
--
-- 【多数情况下你应该用一键版，而不是这个文件】
--     python3 scripts/diagnose_identity.py --org-id <你的org_id> \
--         --channel 官网 --external-id zhangwei
--   一键版自己连库、自己跑完五层、直接告诉你断在哪、怎么修。
--
-- 这个 .sql 留给两种情况：
--   ① 你只能通过 DBA / 运维代跑，交给他们一段能直接执行的 SQL 更省事
--   ② 一键版的结论你不放心，想自己看原始数据
--
-- 用法：把 org_data_xxx 换成你的 schema，把 'zhangwei' 换成实际 external_id，
--       逐段执行。每段下面的「判读」告诉你这一层通没通，通了就往下一段。

-- ────────────────────────────────────────────────────────────
-- 第 0 层：identities 里到底有没有这条映射，key 长什么样
-- ────────────────────────────────────────────────────────────
SELECT identity_key,
       cust_id,
       merge_state,
       confidence,
       -- 下面三列是关键：肉眼看不出的差异在这里现形
       length(identity_key)                              AS key_len,
       identity_key = ('官网' || ':' || 'zhangwei')       AS exact_match_expected,
       encode(convert_to(identity_key, 'UTF8'), 'hex')   AS key_hex
FROM org_data_xxx."crm_identities"
WHERE identity_key ILIKE '%zhangwei%';
-- 判读：
--   查不到           → 映射不存在，用户"未绑定"的判断成立，平台侧补绑
--   查到且 cust_id 非空 → 映射在位，往下查为什么没被读到
--   exact_match_expected = false → 字面不等！看 key_hex 找不可见字符/全角冒号/
--                                  大小写差异。这是最常见的"数据在位但读不到"

-- ────────────────────────────────────────────────────────────
-- 第 1 层：会话行本身。cust_id 是不是真空，channel 存的什么值
-- ────────────────────────────────────────────────────────────
SELECT conv_id, cust_id, channel, external_id, state, turn_count,
       started_at, updated_at,
       cust_id IS NULL AS cust_id_is_null
FROM org_data_xxx."crm_conversations"
ORDER BY started_at DESC NULLS LAST
LIMIT 5;
-- 判读：
--   channel 值 ≠ 第0层 identity_key 的渠道前缀 → 渠道命名不一致，这就是根因
--   external_id 为空 → inbound_gateway 没收到 external_id，识别链路从源头就断了
--   cust_id 为空但第0层映射在位 → identity_resolve 没跑，或跑了没回填

-- ────────────────────────────────────────────────────────────
-- 第 2 层：identity_resolve 到底有没有被调用过
-- 它命中时会写 merge_evidence，没有任何痕迹就是压根没跑
-- ────────────────────────────────────────────────────────────
SELECT identity_key, cust_id, merge_state,
       left(merge_evidence, 200) AS evidence_head,
       created_at
FROM org_data_xxx."crm_identities"
WHERE created_at >= now() - interval '1 day'
ORDER BY created_at DESC
LIMIT 20;
-- 判读：
--   测试时段内毫无新增/更新 → identity_resolve 未被调用（角色编排问题，不是技能问题）
--   有 merge_evidence 且含 new_customer:true → 它没命中已有映射，反而新建了客户
--                                              → 说明 identity_key 拼接对不上

-- ────────────────────────────────────────────────────────────
-- 第 3 层：有没有因为认不出而重复建档（污染扩散程度）
-- ────────────────────────────────────────────────────────────
SELECT c.cust_id, c.name, c.phone, c.source_channel, c.created_at,
       (SELECT count(*) FROM org_data_xxx."crm_identities" i
         WHERE i.cust_id = c.cust_id) AS identity_count
FROM org_data_xxx."crm_customers" c
WHERE c.created_at >= now() - interval '1 day'
  AND COALESCE(c.is_deleted, FALSE) = FALSE
ORDER BY c.created_at DESC;
-- 判读：
--   出现 name 形如「官网用户」的新客户 → 识别失败已经在造垃圾档案，
--   每测一轮多一条，修好识别后要清理

-- ────────────────────────────────────────────────────────────
-- 第 4 层：核身话术的来源。查订单在手册里是只读免确认动作
-- ────────────────────────────────────────────────────────────
SELECT * FROM org_data_xxx."crm_actions"
WHERE created_at >= now() - interval '1 day'
ORDER BY created_at DESC LIMIT 10;
-- 判读：
--   没有「查订单状态」的台账行 → action_execute 根本没被调用，
--   那句"请提供订单编号或手机号"是模型自己编的流程（见分析第二层问题）
