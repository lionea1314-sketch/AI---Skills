# Skills

本目录存放本仓库的 Claude Code Skills。每个技能是一个独立目录,目录名即技能名。

## 技能清单

| 技能 | 用途 | 什么时候触发 |
|---|---|---|
| [`detail-master`](./detail-master/) | 电商详情页 / Listing 文案:标题、五点、描述、A+、图片脚本、埋词、合规检查 | 用户要写或优化商品文案、诊断详情页转化 |
| [`amazon-kit`](./amazon-kit/) | 亚马逊运营数据:广告报表、搜索词报告、ABA、评论 VOC、费用测算 | 用户丢来后台导出的报表,或要算 ACOS / 否词 / 竞品分析 |

两者分工:`amazon-kit` 出数据和结论,`detail-master` 把结论落成文案。

## 目录规范

```
skills/
└── <skill-name>/
    ├── SKILL.md      # 必需:YAML frontmatter + 正文
    ├── scripts/      # 可选:可执行脚本,处理确定性/重复性工作
    ├── references/   # 可选:按需加载的参考文档
    └── assets/       # 可选:输出中用到的模板、图标、字体
```

`scripts/`、`references/`、`assets/` 按需创建,不需要就不要留空目录。

## SKILL.md 格式

```markdown
---
name: skill-name
description: 技能做什么 + 什么时候用。这是唯一的触发依据。
---

# 正文
```

约定:

- `name` 必须是小写字母、数字和连字符,且与所在目录名完全一致。
- `description` 是技能能否被调用的**唯一**依据,所有"什么时候用"的信息都要写在这里,不要留在正文里。写得具体一点、主动一点 —— Claude 更容易漏调用技能而不是过度调用,所以要把用户可能说出口的关键词都覆盖到(中英文都写)。
- 正文控制在 500 行以内。内容更多时拆到 `references/`,并在 SKILL.md 里说明什么情况下去读哪个文件。
- 用祈使句写指令,并解释**为什么** —— 讲清楚原因比堆一串大写的 MUST 更有效。

## 校验

```bash
python3 scripts/validate_skills.py
```

检查每个技能目录都有 SKILL.md、frontmatter 完整、`name` 与目录名一致。

## 本地安装

Claude Code 从 `.claude/skills/` 读取技能。选一种方式:

```bash
# 项目级(只在这个仓库生效)
mkdir -p .claude/skills && ln -s ../../skills/detail-master .claude/skills/detail-master

# 用户级(所有项目生效)
ln -s "$(pwd)/skills/detail-master" ~/.claude/skills/detail-master
```

用 `ln -s` 而不是 `cp`,这样改完源文件立刻生效,不用记得同步。

## 新增技能

1. `mkdir skills/<name>`,写 `SKILL.md`
2. 跑 `python3 scripts/validate_skills.py`
3. 在上面的技能清单里加一行
4. 找 2–3 个真实用户会说的话当测试用例,实际跑一遍看技能有没有被触发、输出对不对
