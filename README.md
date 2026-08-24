# AI Skills

一组用于 [Claude Code](https://claude.com/claude-code) 的 Skills,聚焦跨境电商 / 亚马逊运营场景。

技能(Skill)是打包好的一套指令 —— 当你的任务命中某个技能的适用范围时,Claude 会自动加载它的工作流程、格式规范和领域约束,不需要你每次都把要求重述一遍。

## 目录结构

```
.
├── skills/                     # 所有技能
│   ├── detail-master/          # 详情页 / Listing 文案
│   │   └── SKILL.md
│   ├── amazon-kit/             # 亚马逊运营数据分析
│   │   └── SKILL.md
│   └── README.md               # 技能清单与编写规范
├── scripts/
│   └── validate_skills.py      # SKILL.md 格式校验
└── README.md
```

## 技能

| 技能 | 用途 |
|---|---|
| **detail-master** | 撰写和优化商品标题、五点描述、产品描述、A+ 内容、图片脚本,做关键词埋词与平台合规检查 |
| **amazon-kit** | 分析广告报表、搜索词报告、ABA 数据、评论 VOC,计算 ACOS / TACOS / 盈亏平衡点,产出可执行的调整动作 |

分工:`amazon-kit` 负责数据和结论,`detail-master` 负责把结论写成文案。

## 快速开始

```bash
git clone https://github.com/lionea1314-sketch/AI---Skills.git
cd AI---Skills

# 装到用户级,所有项目可用
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/detail-master" ~/.claude/skills/detail-master
ln -s "$(pwd)/skills/amazon-kit"    ~/.claude/skills/amazon-kit
```

装好后直接描述任务即可,不用点名技能:

- "帮我看看这个保温杯的 listing,转化率一直上不去" → detail-master
- "这是上个月的搜索词报告,哪些词该否掉" → amazon-kit

## 开发

新增或修改技能前,先读 [`skills/README.md`](./skills/README.md) 里的编写规范。

```bash
python3 scripts/validate_skills.py
```
