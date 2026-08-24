#!/usr/bin/env python3
"""从 SKILL.md 的「规则」表生成 rules.default.json。

背景：手册 3.1 把 rules.default.json 列为技能必需文件，装载时逐条 upsert 进
crm_rules（已存在的不覆盖，保留企业改过的值）。本仓库约定技能目录只放
SKILL.md + handler.py，规则内容并入 SKILL.md 的「规则」章节——信息是全的，
只是没单独成文件。

平台装载器若按手册校验四件套，缺这个文件就注册不上。这个脚本把它按需生成
出来，不用手工维护两份规则、也不用改仓库约定。

本仓库约定 rules.default.json 为可选文件：需要它时用 --in-place 直接生成到
技能目录，平时也可以只生成到 build/rules/ 预览。生成后 validate_skills.py
会逐字段比对它和 SKILL.md 的规则表，两边漂移就报错——避免出现
"文档写默认 300、实际注入 500"这种查不出来的事故。

用法：
    python3 scripts/gen_rules_default.py                    # 全部技能 → build/rules/
    python3 scripts/gen_rules_default.py --skill read_view  # 只生成一个
    python3 scripts/gen_rules_default.py --in-place         # 写进技能目录
    python3 scripts/gen_rules_default.py --check            # 只校验不写，缺字段报错
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
DEFAULT_OUT = ROOT / "build" / "rules"

# SKILL.md 表头中文名 → rules.default.json 字段。
# 三种表头变体（带/不带「范围」、handoff_pack 的「调整影响」）都在这里收敛。
HEADER_MAP = {
    "rule_key": "rule_key",
    "规则名": "rule_name",
    "类型": "rule_type",
    "默认": "default_value",
    "范围": "range",
    "风险": "risk",
    "含义": "meaning",
    "调高会怎样": "effect_up",
    "调高/开会怎样": "effect_up",
    "调低会怎样": "effect_down",
    "调低/关会怎样": "effect_down",
    "调整影响": "effect_note",      # 没有 up/down 之分，单独存，生成时告警
}

# 手册 3.1 的四要素：含义、调高、调低、风险。缺任何一个，
# 业务人员就没法自己判断该不该改、改成多少。
REQUIRED = ("rule_key", "rule_name", "default_value", "risk", "meaning")
FOUR_ELEMENTS = ("meaning", "effect_up", "effect_down", "risk")


def clean(cell: str) -> str:
    """去掉 markdown 修饰：反引号、加粗、行内注释。"""
    s = cell.strip()
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\*\*([^*]*)\*\*", r"\1", s)
    return s.strip()


def parse_rules(md: str) -> tuple[list, list]:
    """抠出「## 规则」章节里的第一张表。返回 (规则列表, 告警列表)。"""
    warnings: list[str] = []
    m = re.search(r"^##\s*规则\s*$(.*?)(?=^##\s|\Z)", md, re.MULTILINE | re.DOTALL)
    if not m:
        return [], ["SKILL.md 没有「## 规则」章节"]
    body = m.group(1)

    lines = [ln for ln in body.split("\n") if ln.strip().startswith("|")]
    if len(lines) < 3:
        return [], ["「规则」章节里没找到表格"]

    header = [clean(c) for c in lines[0].strip().strip("|").split("|")]
    unknown = [h for h in header if h not in HEADER_MAP]
    if unknown:
        warnings.append(f"表头有未知列 {unknown}，已忽略")
    fields = [HEADER_MAP.get(h) for h in header]

    rules = []
    for ln in lines[2:]:                        # 跳过表头与分隔行
        cells = [clean(c) for c in ln.strip().strip("|").split("|")]
        if len(cells) != len(fields):
            continue
        row = {f: v for f, v in zip(fields, cells) if f}
        if not row.get("rule_key") or "." not in row["rule_key"]:
            continue                            # 不是规则行（说明性表格）
        rules.append(row)
    return rules, warnings


def build(skill_dir: Path) -> tuple[list, list]:
    md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    rules, warnings = parse_rules(md)
    out = []
    for r in rules:
        # 「调整影响」没有 up/down 之分，如实落到 effect_up 并标注来源，
        # 不编造 effect_down——四要素缺了就该被看见，不该被填满看起来齐整。
        note = r.pop("effect_note", "")
        if note and not r.get("effect_up"):
            r["effect_up"] = note
            warnings.append(f"{r['rule_key']}：表里只有「调整影响」，"
                            f"已填入 effect_up，effect_down 留空待补")
        entry = {
            "rule_key": r.get("rule_key", ""),
            "owner_skill": skill_dir.name,
            "rule_name": r.get("rule_name", ""),
            "rule_type": r.get("rule_type", ""),
            "default_value": r.get("default_value", ""),
            "range": r.get("range", ""),
            "risk": r.get("risk", ""),
            "meaning": r.get("meaning", ""),
            "effect_up": r.get("effect_up", ""),
            "effect_down": r.get("effect_down", ""),
        }
        missing = [k for k in REQUIRED if not entry.get(k)]
        if missing:
            warnings.append(f"{entry['rule_key']}：缺必需字段 {missing}")
        thin = [k for k in FOUR_ELEMENTS if not entry.get(k)]
        if thin:
            warnings.append(f"{entry['rule_key']}：四要素缺 {thin}，"
                            f"业务人员无法自行判断该不该改")
        out.append(entry)
    return out, warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="从 SKILL.md 规则表生成 rules.default.json")
    ap.add_argument("--skill", help="只处理这一个技能，缺省处理全部")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT), help="输出根目录")
    ap.add_argument("--in-place", action="store_true",
                    help="写进技能目录（会让 validate_skills.py 判为违规）")
    ap.add_argument("--check", action="store_true", help="只校验不写文件")
    args = ap.parse_args()

    managed = SKILLS_DIR / ".managed"
    names = set()
    if managed.is_file():
        names = {ln.strip() for ln in managed.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.startswith("#")}

    dirs = sorted(d for d in SKILLS_DIR.iterdir()
                  if d.is_dir() and (d / "SKILL.md").is_file()
                  and (not names or d.name in names))
    if args.skill:
        dirs = [d for d in dirs if d.name == args.skill]
        if not dirs:
            print(f"找不到技能 {args.skill}")
            return 1

    total_rules, total_warn, failed = 0, 0, 0
    for d in dirs:
        rules, warnings = build(d)
        total_rules += len(rules)
        total_warn += len(warnings)
        if not rules:
            print(f"FAIL  {d.name}: 一条规则都没解析出来")
            failed += 1
            for w in warnings:
                print(f"      warn: {w}")
            continue

        if args.check:
            print(f"{'WARN' if warnings else ' OK '}  {d.name}: {len(rules)} 条规则")
        else:
            target = d if args.in_place else Path(args.out_dir) / d.name
            target.mkdir(parents=True, exist_ok=True)
            path = target / "rules.default.json"
            path.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
            print(f"{'WARN' if warnings else ' OK '}  {d.name}: {len(rules)} 条 → {path.relative_to(ROOT)}")
        for w in warnings:
            print(f"      warn: {w}")

    print(f"\n{len(dirs)} 个技能，{total_rules} 条规则，{total_warn} 个告警")
    if args.in_place and not args.check:
        print("已写进技能目录。改了 SKILL.md 的规则表后要重跑一次——"
              "validate_skills.py 会逐字段比对两边，漂移了会报错")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
