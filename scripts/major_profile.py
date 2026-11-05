#!/usr/bin/env python3
"""第 1 步：专业画像 —— 「这个专业读研干什么、之后读博或工作什么样」。

四个块，每块都必须有来源；查不到就是 unknown，绝不写「前景广阔」：
  A 定义（学科目录口径，离线可查）
  B1 读研做什么（共性：培养目标/学制/论文要求）
  B2 读研做什么（各校研究方向，按学校逐条记）
  C 能不能往上读（博士点/硕博连读/双一流学科）
  D 毕业去哪（就业质量报告）

用法：
  python major_profile.py --code 085401              # 看画像 + 缺口清单
  python major_profile.py --code 085401 --init       # 生成骨架，A 块自动填好
  python major_profile.py --name 新一代电子信息技术      # 按名称反查代码
  python major_profile.py --code 085401 --check      # 只输出「还缺什么、去哪查」
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# v0.4.0：默认数据目录放到技能**外面**（同级 kaoyan-data/），技能包保持只读干净。
# 设了 KAOYAN_HOME 就听它的。
DATA_ROOT = os.path.abspath(os.environ.get("KAOYAN_HOME")
                            or os.path.join(os.path.dirname(ROOT), "kaoyan-data"))
CODES_CSV = os.path.join(ROOT, "data", "major_codes.csv")
MAJORS_DIR = os.path.join(DATA_ROOT, "data", "majors")
TEMPLATE = os.path.join(ROOT, "assets", "major_profile.template.md")

SECTIONS = [
    ("A", "定义（学科目录口径）"),
    ("B1", "读研做什么（共性）"),
    ("B2", "读研做什么（各校差异）"),
    ("C", "能不能往上读"),
    ("D", "毕业去哪"),
]
# 每个缺失字段该去哪找（写死在代码里，用户补料时直接照抄）
WHERE = {
    "培养目标（官方表述）": "专业学位教指委指导性培养方案 / 各校研究生院「培养方案」",
    "基本学制": "各校招生章程（搜「学制」）",
    "是否要求发表论文": "学校学位授予细则",
    "是否有企业联培 / 实践学分": "培养方案 / 招生简章",
    "是否有一级学科博士点": "学校「学科建设」页 / 研究生院学位点清单",
    "能否硕博连读 / 直博": "学校研究生院「硕博连读」「直博」办法",
    "是否「双一流」建设学科": "教育部双一流建设学科名单",
    "主要去向（行业/单位类型）": "学校《毕业生就业质量报告》（通常 PDF，找到对应学院那几页）",
    "升学（读博）比例": "同上",
    "报告年份": "同上",
    "与相邻专业的关系": "《学位授予和人才培养学科目录》/ 研招网专业库",
    "研究方向（照抄目录）": "该校当年招生专业目录（研究方向列）",
}


def load_codes() -> list[dict]:
    if not os.path.exists(CODES_CSV):
        return []
    with open(CODES_CSV, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def find_code(args) -> dict | None:
    codes = load_codes()
    if args.code:
        # 少写前导零也要认：85401 → 085401（和 check.py 的 norm_code 一致）
        norm = re.sub(r"\D", "", args.code).zfill(6)
        for c in codes:
            if re.sub(r"\D", "", c["code"]).zfill(6) == norm:
                return c
        return None
    if args.name:
        for c in codes:
            if args.name in c["name"] or c["name"] in args.name:
                return c
    return None


def parse_profile(path: str) -> dict[str, list[tuple[str, str, str]]]:
    """把画像 md 拆成 {区块: [(字段, 内容, 来源)]}，只取表格行。"""
    out: dict[str, list[tuple[str, str, str]]] = {}
    if not os.path.exists(path):
        return out
    cur = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("## "):
                head = line[3:].strip()
                cur = head.split()[0] if head else None
                for key, _ in SECTIONS:
                    if head.startswith(key):
                        cur = key
                        break
                out.setdefault(cur or "_", [])
                continue
            if cur and line.startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 2 or set("".join(cells)) <= set("-: "):
                    continue
                if cells[0] in ("项", "学校", "日期"):
                    continue
                out[cur].append(tuple(cells + [""] * (3 - len(cells)))[:3])
    return out


def is_unknown(v: str) -> bool:
    v = (v or "").strip().lower()
    return v in ("", "unknown", "—", "-", "待查")


def one_liner(code: dict) -> str:
    return (f"{code['code']} {code['name']} —— {code['category']} · {code['degree']}，"
            f"属 {code['major_class']}。{code.get('note', '')}")


# 每个区块应有的字段（骨架里没有、或整块缺失时，按这个清单报缺口）
SECTION_FIELDS = {
    "A": ["与相邻专业的关系"],
    "B1": ["培养目标（官方表述）", "基本学制", "是否要求发表论文", "是否有企业联培 / 实践学分"],
    "B2": ["研究方向（照抄目录）"],
    "C": ["是否有一级学科博士点", "能否硕博连读 / 直博", "是否「双一流」建设学科"],
    "D": ["主要去向（行业/单位类型）", "升学（读博）比例", "报告年份"],
}


def gaps(profile: dict[str, list[tuple[str, str, str]]], code: dict) -> list[tuple[str, str, str]]:
    """→ [(区块, 字段, 去哪找)]。整块缺失时按 SECTION_FIELDS 补齐，不许静默当成"没这项"。"""
    out = []
    for key, title in SECTIONS:
        rows = profile.get(key, [])
        if key == "B2":
            if not any(r[1] and not is_unknown(r[1]) for r in rows):
                out.append((key, "各校研究方向", WHERE["研究方向（照抄目录）"]))
            continue
        if not rows:
            for field in SECTION_FIELDS[key]:
                out.append((key, field, WHERE.get(field, "见 references/majors.md")))
            continue
        for name, value, src in rows:
            if name in ("专业代码", "专业名称", "所属类别", "学位类型", "学科门类"):
                continue
            if is_unknown(value):
                out.append((key, name, WHERE.get(name, "见 references/majors.md")))
    return out


def render(code: dict, profile: dict, gap_list: list) -> str:
    L = [f"# {code['code']} {code['name']}", ""]
    L.append(f"> {code['category']} · {code['degree']} · {code['major_class']}"
             f"｜生成：{dt.date.today()}")
    restriction = (code.get("restriction") or "").strip()
    if restriction:
        L.append("")
        L.append(f"> 🚫 **报考资格硬门槛：{restriction}**。"
                 f"这条不符，后面查多少数据都没用——先确认你的本科专业。")
    if not profile:
        L.append("> ⚠️ 还没有画像文件，下面只有 A 块（学科目录口径）；用 `--init` 生成骨架再补。")
    L.append("")
    L.append("## 一句话")
    L.append("")
    L.append(one_liner(code))
    L.append("")
    for key, title in SECTIONS:
        rows = profile.get(key, [])
        filled = [r for r in rows if r[1] and not is_unknown(r[1])]
        L.append(f"## {key} {title}")
        L.append("")
        if key == "B2":
            if filled:
                L.append("| 学校 | 研究方向 | 来源 |")
                L.append("|---|---|---|")
                for r in filled:
                    L.append(f"| {r[0]} | {r[1]} | {r[2] or '—'} |")
            else:
                L.append("_未录入。这一块按「学校 × 研究方向」记，来自各校招生目录的研究方向列。_")
        else:
            if not rows:      # 整块缺失也要把该有的字段列成 unknown，不能只写"尚未录入"
                rows = [(f, "unknown", "") for f in SECTION_FIELDS[key]]
            L.append("| 项 | 内容 | 来源 |")
            L.append("|---|---|---|")
            for r in rows:
                mark = " ⚠️" if is_unknown(r[1]) else ""
                L.append(f"| {r[0]} | {r[1] or 'unknown'}{mark} | {r[2] or '—'} |")
        L.append("")
    if gap_list:
        L.append(f"## 还缺 {len(gap_list)} 项 —— 去哪找")
        L.append("")
        L.append("| 块 | 字段 | 去哪找 |")
        L.append("|---|---|---|")
        for key, field, where in gap_list:
            L.append(f"| {key} | {field} | {where} |")
        L.append("")
        L.append("拿到后按下面格式回填（`data/majors/"
                 f"{code['code']}.md`），标来源与谁提供：")
        L.append("")
        L.append("```")
        for key, field, _ in gap_list[:6]:
            L.append(f"| {field} | （填内容） | 官方｜<文件或网址>｜<日期> |")
        L.append("```")
    else:
        L.append("## 四项都填齐了")
        L.append("")
        L.append("下一步：用 `check.py` 查具体学校（画像只说明「这专业做什么」，不说明「哪所好考」）。")
    return "\n".join(L)


def init_file(code: dict) -> str:
    with open(TEMPLATE, encoding="utf-8") as fh:
        tpl = fh.read()
    md = (tpl.replace("{{code}}", code["code"]).replace("{{name}}", code["name"])
             .replace("{{category}}", code["category"]).replace("{{degree}}", code["degree"])
             .replace("{{major_class}}", code["major_class"])
             .replace("{{one_liner}}", one_liner(code))
             .replace("{{date}}", str(dt.date.today())))
    os.makedirs(MAJORS_DIR, exist_ok=True)
    path = os.path.join(MAJORS_DIR, f"{code['code']}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="专业画像：读研做什么 / 往上读 / 毕业去哪")
    ap.add_argument("--code", help="专业代码，如 085401")
    ap.add_argument("--name", help="专业名称（模糊）")
    ap.add_argument("--init", action="store_true", help="生成画像骨架（A 块自动填）")
    ap.add_argument("--check", action="store_true", help="只输出缺口清单")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    code = find_code(args)
    if not code:
        print("没找到这个专业代码。可用：", file=sys.stderr)
        for c in load_codes():
            print(f"  {c['code']} {c['name']}（{c['degree']}）", file=sys.stderr)
        return 2

    if args.init:
        path = init_file(code)
        print(f"已生成骨架：{os.path.relpath(path, DATA_ROOT)}")

    profile_path = os.path.join(MAJORS_DIR, f"{code['code']}.md")
    profile = parse_profile(profile_path)
    gap_list = gaps(profile, code)

    if args.json:
        print(json.dumps({"code": code, "one_liner": one_liner(code),
                          "sections": {k: v for k, v in profile.items()},
                          "gaps": [{"section": k, "field": f, "where": w} for k, f, w in gap_list]},
                         ensure_ascii=False, indent=2))
        return 0
    if args.check:
        print(f"{code['code']} {code['name']}：还缺 {len(gap_list)} 项")
        for key, field, where in gap_list:
            print(f"  [{key}] {field} → {where}")
        return 0 if not gap_list else 1

    print(render(code, profile, gap_list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

