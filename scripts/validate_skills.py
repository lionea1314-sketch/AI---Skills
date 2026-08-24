#!/usr/bin/env python3
"""校验 skills/ 下每个技能的 SKILL.md 是否符合规范。

用法: python3 scripts/validate_skills.py
退出码 0 表示全部通过,1 表示有错误。
"""
import re
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text):
    """提取 frontmatter 的顶层 key: value。不引入 yaml 依赖,格式简单够用。"""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields, key = {}, None
    for line in m.group(1).split("\n"):
        kv = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            fields[key] = kv.group(2).strip()
        elif key and line.strip():  # 续行
            fields[key] += " " + line.strip()
    return fields


def check(skill_dir):
    errors, warnings = [], []
    name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.is_file():
        return [f"{name}: 缺少 SKILL.md"], []

    fm = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    if fm is None:
        return [f"{name}: SKILL.md 开头缺少 --- 包裹的 frontmatter"], []

    if not NAME_RE.match(name):
        errors.append(f"{name}: 目录名必须是小写字母/数字/连字符")
    if "name" not in fm:
        errors.append(f"{name}: frontmatter 缺少 name")
    elif fm["name"] != name:
        errors.append(f"{name}: frontmatter name '{fm['name']}' 与目录名不一致")
    if not fm.get("description"):
        errors.append(f"{name}: frontmatter 缺少 description")
    elif len(fm["description"]) < 40:
        warnings.append(f"{name}: description 偏短,可能不足以让技能被正确触发")

    body_lines = len(skill_md.read_text(encoding="utf-8").split("\n"))
    if body_lines > 500:
        warnings.append(f"{name}: SKILL.md {body_lines} 行,建议拆分到 references/")

    return errors, warnings


def main():
    if not SKILLS_DIR.is_dir():
        print(f"找不到目录: {SKILLS_DIR}")
        return 1

    dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    if not dirs:
        print("skills/ 下没有技能目录")
        return 1

    all_errors, all_warnings = [], []
    for d in dirs:
        errors, warnings = check(d)
        all_errors += errors
        all_warnings += warnings
        print(f"{'FAIL' if errors else ' OK ':>4}  {d.name}")

    for w in all_warnings:
        print(f"warn: {w}")
    for e in all_errors:
        print(f"error: {e}")

    print(f"\n{len(dirs)} 个技能,{len(all_errors)} 个错误,{len(all_warnings)} 个警告")
    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
