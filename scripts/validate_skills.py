#!/usr/bin/env python3
"""校验 skills/ 下每个技能是否符合《AI 客户经营系统 · 部署手册 v4》第 3.1 节规范。

检查项：
  1. 目录只含 SKILL.md 与 handler.py（本仓库约定：技能文件只有这两个）
  2. SKILL.md frontmatter 可被严格 YAML 解析，字段集与顺序对齐元数据模板
  3. name 与目录名一致；category / trigger-type / cost-tier 取值合法
  4. positive-examples / negative-examples 非空（决定能否被正确召回）
  5. handler.py 存在 async def execute(input_data, runtime_context)
  6. handler.py 不跨技能 import（施工规范第 13 条）
  7. handler.py 无 :name::type 写法（施工规范第 5 条）
  8. SKILL.md 含「施工规范符合性自查」章节

用法: python3 scripts/validate_skills.py [--strict]
  --strict 时警告也算失败。
退出码 0 表示通过，1 表示有错误。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"

TEMPLATE_FIELDS = ["name", "description", "name_zh", "source", "category",
                   "trigger-type", "cost-tier", "tags",
                   "positive-examples", "negative-examples", "domain"]
CATEGORIES = {"intake", "answer", "action", "safety", "analysis", "training", "data"}
TRIGGERS = {"explicit", "auto", "cron", "sidecar"}
COST_TIERS = {"zero", "low", "medium", "high"}
SOURCES = {"builtin", "external", "learned", "mcp"}

ALLOWED_FILES = {"SKILL.md", "handler.py"}
NAME_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
UNSAFE_CAST_RE = re.compile(r":\w+::")
EXECUTE_RE = re.compile(r"async\s+def\s+execute\s*\(\s*input_data\s*:?[^,]*,\s*runtime_context")
CROSS_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(?:skills?\.|\.\.)", re.MULTILINE)


def parse_frontmatter(text: str):
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, "开头缺少 --- 包裹的 frontmatter"
    try:
        import yaml
    except ImportError:
        return None, "需要 pyyaml：pip install pyyaml"
    try:
        data = yaml.safe_load(m.group(1))
    except Exception as e:                           # noqa: BLE001
        return None, f"YAML 解析失败：{e}"
    if not isinstance(data, dict):
        return None, "frontmatter 不是键值映射"
    return data, ""


def check_skill(skill_dir: Path):
    errors, warnings = [], []
    name = skill_dir.name

    extra = {p.name for p in skill_dir.iterdir() if p.is_file()} - ALLOWED_FILES
    if extra:
        errors.append(f"目录含约定外文件 {sorted(extra)}（技能只应有 SKILL.md 与 handler.py）")
    subdirs = [p.name for p in skill_dir.iterdir()
               if p.is_dir() and p.name != "__pycache__"]
    if subdirs:
        errors.append(f"目录含子目录 {sorted(subdirs)}（技能只应有 SKILL.md 与 handler.py）")

    skill_md = skill_dir / "SKILL.md"
    handler = skill_dir / "handler.py"
    if not skill_md.is_file():
        return [f"{name}: 缺少 SKILL.md"], warnings
    if not handler.is_file():
        errors.append("缺少 handler.py")

    if not NAME_RE.match(name):
        errors.append(f"目录名 {name!r} 不合规：英文小写下划线")

    md_text = skill_md.read_text(encoding="utf-8")
    fm, err = parse_frontmatter(md_text)
    if fm is None:
        errors.append(err)
    else:
        missing = [k for k in TEMPLATE_FIELDS if k not in fm]
        if missing:
            errors.append(f"frontmatter 缺字段 {missing}")
        order = [k for k in fm if k in TEMPLATE_FIELDS]
        if order and order != [k for k in TEMPLATE_FIELDS if k in fm]:
            warnings.append(f"字段顺序偏离模板：{order}")
        if fm.get("name") != name:
            errors.append(f"frontmatter name {fm.get('name')!r} 与目录名不一致")
        if fm.get("source") and fm["source"] not in SOURCES:
            errors.append(f"source 非法：{fm['source']}（应为 {sorted(SOURCES)}）")
        if fm.get("category") not in CATEGORIES:
            errors.append(f"category 非法：{fm.get('category')}（应为 {sorted(CATEGORIES)}）")
        if fm.get("trigger-type") not in TRIGGERS:
            errors.append(f"trigger-type 非法：{fm.get('trigger-type')}（应为 {sorted(TRIGGERS)}）")
        if fm.get("cost-tier") not in COST_TIERS:
            errors.append(f"cost-tier 非法：{fm.get('cost-tier')}（应为 {sorted(COST_TIERS)}）")
        if not fm.get("positive-examples"):
            errors.append("positive-examples 为空，技能无法被正确召回")
        if not fm.get("negative-examples"):
            errors.append("negative-examples 为空，容易被相邻技能误召回")
        desc = str(fm.get("description") or "")
        if len(desc) < 20:
            warnings.append("description 偏短，触发准确率会受影响")

    if "施工规范符合性自查" not in md_text:
        warnings.append("SKILL.md 缺少「施工规范符合性自查」章节")
    if "## 规则" not in md_text:
        warnings.append("SKILL.md 缺少「规则」章节（应列出全部 rule_key）")

    if handler.is_file():
        py = handler.read_text(encoding="utf-8")
        if not EXECUTE_RE.search(py):
            errors.append("handler.py 缺少 async def execute(input_data, runtime_context)")
        if CROSS_IMPORT_RE.search(py):
            errors.append("handler.py 存在跨技能 import（施工规范第 13 条）")
        lines = py.split("\n")
        for m in UNSAFE_CAST_RE.finditer(py):
            no = py[:m.start()].count("\n") + 1
            src = lines[no - 1]
            # 防呆正则的定义行、以及规范文档里提到的字面占位串 :name::type
            # 都不是真实 SQL，跳过；真实违规形如 :cust_id::text 仍会被抓到。
            if "re.compile" in src or ":name::type" in src:
                continue
            errors.append(f"handler.py:{no} 出现 :name::type 写法（第 5 条）")

    return [f"{name}: {e}" for e in errors], [f"{name}: {w}" for w in warnings]


def _managed_set() -> set:
    """读取 skills/.managed 纳管清单。文件不存在时校验全部目录。"""
    f = SKILLS_DIR / ".managed"
    if not f.is_file():
        return set()
    return {ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def main() -> int:
    strict = "--strict" in sys.argv
    if not SKILLS_DIR.is_dir():
        print(f"找不到目录：{SKILLS_DIR}")
        return 1

    dirs = sorted(d for d in SKILLS_DIR.iterdir()
                  if d.is_dir() and d.name != "__pycache__")
    if not dirs:
        print("skills/ 下没有技能目录")
        return 1

    managed = _managed_set()
    all_errors, all_warnings, legacy = [], [], []
    for d in dirs:
        if managed and d.name not in managed:
            legacy.append(d.name)
            print(f"SKIP  {d.name}  (legacy，未纳入手册体系)")
            continue
        errors, warnings = check_skill(d)
        all_errors += errors
        all_warnings += warnings
        print(f"{'FAIL' if errors else 'WARN' if warnings else ' OK '}  {d.name}")

    for w in all_warnings:
        print(f"warn : {w}")
    for e in all_errors:
        print(f"error: {e}")

    checked = len(dirs) - len(legacy)
    print(f"\n纳管 {checked} 个技能，{len(all_errors)} 个错误，{len(all_warnings)} 个警告"
          + (f"；跳过 legacy {len(legacy)} 个：{', '.join(legacy)}" if legacy else ""))
    return 1 if all_errors or (strict and all_warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
