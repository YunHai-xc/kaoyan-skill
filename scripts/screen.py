#!/usr/bin/env python3
"""按约束筛出候选短名单。

做三件事，**不做第四件**：
1. 从 `data/pool.csv`（研招网院校库）按省市 / 层次筛
2. 交叉 `data/schools.csv`：哪些学校已有三年数据、结论是什么
3. 输出短名单，每所写清「还缺什么、下一步怎么查」

**不做的是：给学校排名。** 没有全国统一的、可复核的院校质量数据，
任何「第 1 名到第 10 名」都是编的。名单只按「约束是否满足」分组。

用法：
  python screen.py --province 浙江 广东 --major 085404
  python screen.py --province 浙江 --level 双一流 --exclude 本科母校名 --top 8
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# v0.4.0：默认数据目录放到技能**外面**（同级 kaoyan-data/），技能包保持只读干净。
# 设了 KAOYAN_HOME 就听它的。
DATA_ROOT = os.path.abspath(os.environ.get("KAOYAN_HOME")
                            or os.path.join(os.path.dirname(ROOT), "kaoyan-data"))
POOL = os.path.join(DATA_ROOT, "data", "pool.csv")
LEDGER = os.path.join(DATA_ROOT, "data", "schools.csv")
FILTERS = os.path.join(DATA_ROOT, "assets", "filters.json")
OUTDIR = os.path.join(DATA_ROOT, "reports")


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def ledger_summary(major: str = "") -> dict[str, dict]:
    """台账里每所学校的已有结论（可按专业代码隔离）。

    指标口径：
      · `fc_pass_rate` = **一志愿上线率 = 上线人数 ÷ 统考录取**（可 >1），不是「录取率」
      · 「一志愿录取率 = 录取 ÷ 上线」只在 note 里有，不混用同一个字段

    v0.2.2：只取**最大年份**那一行。最大年份的字段是 `unknown` 时显示 `—`，
    **不许沿用旧年份的数值**——那会把 2025 的数据冒充成 2026 的。
    """
    rows = []
    for r in read_csv(LEDGER):
        name = (r.get("school") or "").strip()
        if not name or name == "unknown":
            continue
        if major and r.get("major_code", "unknown") != major:
            continue
        try:
            yv = int(str(r.get("enroll_year") or "0").strip() or 0)
        except ValueError:
            yv = 0
        rows.append((name, yv, r))

    latest: dict[str, int] = {}
    years: dict[str, set] = {}
    majors: dict[str, set] = {}
    for name, yv, r in rows:
        latest[name] = max(latest.get(name, -1), yv)
        years.setdefault(name, set()).add(r.get("enroll_year", ""))
        majors.setdefault(name, set()).add(r.get("major_code", ""))

    out: dict[str, dict] = {}
    for name, yv, r in rows:
        d = out.setdefault(name, {"years": set(), "exempt": None, "online_rate": None,
                                  "majors": set(), "latest_year": latest[name]})
        d["years"] |= years[name]
        d["majors"] |= majors[name]
        if yv != latest[name]:
            continue                       # 只看最新年，旧行不参与
        fr = r.get("fc_pass_rate")
        if fr not in (None, "", "unknown"):
            try:
                d["online_rate"] = float(fr)
            except ValueError:
                pass
        ex = r.get("plan_exempt")
        if ex not in (None, "", "unknown"):
            try:
                d["exempt"] = int(float(ex))
            except ValueError:
                pass
    for d in out.values():
        d["years"] = sorted(y for y in d["years"] if y)
        d["majors"] = sorted(m for m in d["majors"] if m and m != "unknown")
        d.pop("_latest_year", None)
        d["years"] = sorted(y for y in d["years"] if y)
        d["majors"] = sorted(m for m in d["majors"] if m and m != "unknown")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--province", nargs="*", help="省份中文名；留空=全部")
    ap.add_argument("--level", default="不限", choices=["不限", "双一流", "非双一流"])
    ap.add_argument("--exclude", nargs="*", default=[], help="要剔除的学校（如本科母校）")
    ap.add_argument("--major", default="", help="专业代码，用于生成下一步命令")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--filters", default=FILTERS, help="filters.json 路径（可选）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if os.path.exists(args.filters):
        with open(args.filters, encoding="utf-8") as fh:
            f = json.load(fh)
        args.province = args.province or f.get("provinces") or []
        if args.level == "不限":
            args.level = f.get("level", "不限")
        args.major = args.major or (f.get("target", {}) or {}).get("major_code", "")
        print(f"已读取 {os.path.relpath(args.filters, ROOT)}")

    pool = read_csv(POOL)
    if not pool:
        print("池子是空的。先跑：python scripts/pool.py")
        return 1

    ledger = ledger_summary(args.major)

    # —— 逐级过滤，并记录每一步砍掉了什么（最后要如实告诉用户）——
    steps = []
    cur = pool
    if args.province:
        cur = [r for r in cur if r["province"] in args.province]
        steps.append(f"限定省市 {'、'.join(args.province)} → {len(cur)} 所")
    if args.level == "双一流":
        cur = [r for r in cur if "双一流" in r.get("tag", "")]
        steps.append(f"只要双一流 → {len(cur)} 所")
    elif args.level == "非双一流":
        cur = [r for r in cur if "双一流" not in r.get("tag", "")]
        steps.append(f"只要非双一流 → {len(cur)} 所")
    if args.exclude:
        cur = [r for r in cur if not any(x and x in r["school"] for x in args.exclude)]
        steps.append(f"剔除 {'、'.join(args.exclude)} → {len(cur)} 所")

    if not cur:
        print("筛完是空的。放宽条件重试（比如去掉 --level 或加上更多省份）。")
        for s in steps:
            print("  ·", s)
        return 2

    # —— 分组（不排名）——
    known = [r for r in cur if r["school"] in ledger]
    fresh = [r for r in cur if r["school"] not in ledger]
    known.sort(key=lambda r: (r["province"], r["school"]))
    fresh.sort(key=lambda r: (r["province"], r["school"]))

    L = []
    L.append(f"> 生成日期：{dt.date.today()}｜范围：{'、'.join(args.province) or '全部省市'}"
             f"｜层次：{args.level}｜专业：{args.major or '未指定'}")
    L.append(f"> 候选池：{len(pool)} 所（研招网院校库）→ 筛后 {len(cur)} 所"
             f"｜已建台账 {len(known)} 所 / 待查 {len(fresh)} 所")
    L.append("")
    L.append("# 候选短名单")
    L.append("")
    L.append("**这份名单不排名。** 只按「约束是否满足」分组——没有全国统一的院校质量数据，"
             "任何名次都是编的。顺序按省份 + 校名，纯为方便查找。")
    L.append("")
    L.append("## 筛的过程（砍掉了什么）")
    L.append("")
    L.append(f"- 起始：研招网院校库 {len(pool)} 所")
    for s in steps:
        L.append(f"- {s}")
    L.append(f"- 其中 {len(known)} 所已建台账（有数据，可直接看结论）")
    L.append("")

    if known:
        L.append("## 一、已有数据的（可直接下判断）")
        L.append("")
        L.append("| 学校 | 省份 | 标签 | 台账覆盖 | 最近年份 | 推免人数 | 一志愿上线率 |")
        L.append("|---|---|---|---:|---:|---:|---:|")
        for r in known:
            d = ledger[r["school"]]
            ex = f"{d['exempt']} 人" if isinstance(d["exempt"], int) else "—"
            fr = f"{d['online_rate']:.2f}×" if isinstance(d["online_rate"], float) else "—"
            ly = str(d.get("latest_year") or "—")
            L.append(f"| {r['school']} | {r['province']} | {r.get('tag', '') or '—'} "
                     f"| {len(d['years'])} 年 | {ly} | {ex} | {fr} |")
        L.append("")
        L.append("> 口径：上线率 = 一志愿上线人数 ÷ 统考录取人数，可大于 1（>1 表示上线人数已超名额）。"
                 "它不等于「录取率」。**只显示最近年份的数值；该年为 unknown 时显示 `—`，不沿用旧年份。**")
        L.append("")

    L.append(f"## 二、待查的（共 {len(fresh)} 所，按需挑 {args.top} 所去查）")
    L.append("")
    L.append("| 学校 | 省份 | 标签 | 下一步 |")
    L.append("|---|---|---|---|")
    for r in fresh[:args.top]:
        cmd = (f"`check.py --school {r['school']}"
               + (f" --major {args.major}" if args.major else "") + " --seed <招生目录页>`")
        L.append(f"| {r['school']} | {r['province']} | {r.get('tag', '') or '—'} | {cmd} |")
    if len(fresh) > args.top:
        L.append(f"| …另有 {len(fresh) - args.top} 所未列出 | | | 调大 --top 查看 |")
    L.append("")
    L.append("## 三、每所都还缺什么")
    L.append("")
    L.append("1. **招生目录入口页（seed）** —— 研招网院校库不给官网链接，每所首次要人工给一次，"
             "给过就记进 `data/sources.csv`")
    L.append("2. **有没有你的专业** —— 池子只到学校粒度，专业要进目录查")
    L.append("3. **钱和住** —— 按 `references/life.md` 查简章第十二条 + 校区/学费公示附表")
    L.append("4. **估分** —— Δ 与冲稳保要等考生侧给数，脚本不代猜")
    L.append("")
    L.append("---")
    L.append("> 池子来源：研招网院校库 `/sch/search.do?ssdm=<省>&start=<分页>`，"
             "标签仅「双一流」一种，不代表学科实力或录取难度。")

    md = "\n".join(L)
    out = args.out or os.path.join(OUTDIR, f"{dt.date.today()}-候选短名单.md")
    os.makedirs(OUTDIR, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md.split("## 一、")[0])
    print(f"→ {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

