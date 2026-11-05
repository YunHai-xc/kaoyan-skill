#!/usr/bin/env python3
"""抽样自检：对一批入口页跑「抓取 → 解析 → 认列」，把成功率量出来。

为什么必须有这个：分发版最该被问的一句话是「它到底能跑通多少比例的学校」。
没有这张表，任何「支持全国院校」的说法都是空的。

它只跑到「能不能解析出表、能不能认出专业代码列」为止，**不判断结论对错**，
因为判断对错需要人工核对原始表格。

用法：
  python scripts/selftest.py                      # 跑 data/seeds.builtin.csv 全部
  python scripts/selftest.py --limit 3            # 只跑前 3 个
  python scripts/selftest.py --major 085401       # 顺带统计该专业代码命中几所
  python scripts/selftest.py --seeds my.csv       # 自定义种子表（列：school,seed）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# v0.4.0：默认数据目录放到技能**外面**（同级 kaoyan-data/），技能包保持只读干净。
# 设了 KAOYAN_HOME 就听它的。
DATA_ROOT = os.path.abspath(os.environ.get("KAOYAN_HOME")
                            or os.path.join(os.path.dirname(ROOT), "kaoyan-data"))
sys.path.insert(0, HERE)

import check as ck                  # noqa: E402
import extract_tables as et         # noqa: E402

STAGES = ["crawl", "download", "parse", "columns"]


def count_tables(tables, major: str) -> tuple[int, int, int]:
    """→ (表数, 能认出专业代码列的表数, 该专业行命中数)。必须走 normalize，
    否则「085404计算机技术」这种同格写法永远认不出代码（v0.1 抽样就栽在这）。"""
    total = code_tables = hits = 0
    for _, df in tables:
        if df.empty:
            continue
        total += 1
        df = et.promote_header(df)
        mapping = et.map_headers([str(c) for c in df.columns])
        df = et.normalize(df, mapping)
        mapping = et.map_headers([str(c) for c in df.columns])
        if "major_code" not in mapping:
            continue
        code_tables += 1
        if major:
            codes = [ck.norm_code(x) for x in df.iloc[:, mapping["major_code"]].tolist()]
            hits += sum(1 for x in codes if x == ck.norm_code(major))
    return total, code_tables, hits


def run_one(school: str, seed: str, major: str, seed_type: str = "") -> dict:
    rec = {"school": school, "seed": seed, "type": seed_type, "crawl": "", "download": "",
           "parse": "", "columns": "", "major_hits": "", "path": "", "elapsed_s": "", "note": ""}
    t0 = time.time()
    out_dir = ck.cache_dir(school)
    try:
        assets, skipped = ck.download_assets(seed, 8, out_dir)
        rec["crawl"] = "ok"
        rec["download"] = str(len(assets))
        if skipped:
            rec["note"] = f"隐私跳过 {len(skipped)} 个"
    except ck.FetchError as exc:
        rec["crawl"] = f"fail:{exc.code}"
        rec["elapsed_s"] = f"{time.time() - t0:.0f}"
        return rec
    except Exception as exc:                        # noqa: BLE001
        rec["crawl"] = f"fail:{type(exc).__name__}"
        rec["elapsed_s"] = f"{time.time() - t0:.0f}"
        return rec

    total = code_tables = hits = 0
    # 路线一：附件（PDF / Excel）
    for a in assets:
        path = os.path.join(out_dir, "assets", a["file"])
        ext = os.path.splitext(path)[1].lower()
        try:
            tabs = et.tables_from_pdf(path) if ext == ".pdf" else et.tables_from_excel(path)
        except Exception:                           # noqa: BLE001
            continue
        t, c, h = count_tables(tabs, major)
        total += t
        code_tables += c
        hits += h
    if code_tables:
        rec["path"] = "附件"

    # 路线二：入口页本身就是 HTML 表格（专页式学校常见，如深大统考计划页）
    if not code_tables:
        try:
            tabs = [(f"html-t{i}", df.astype(str))
                    for i, df in enumerate(et.read_html_tables(seed), 1)]
            t, c, h = count_tables(tabs, major)
            if c:
                total, code_tables, hits = t, c, h
                rec["path"] = "HTML表"
        except Exception:                           # noqa: BLE001
            pass

    rec["parse"] = str(total)
    rec["columns"] = f"{code_tables}/{total}" if total else "0/0"
    rec["major_hits"] = str(hits) if major else ""
    rec["elapsed_s"] = f"{time.time() - t0:.0f}"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default=os.path.join(ROOT, "data", "seeds.builtin.csv"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--major", default="")
    ap.add_argument("--out-csv", default=os.path.join(DATA_ROOT, "data", "selftest.csv"))
    args = ap.parse_args()

    if not os.path.exists(args.seeds):
        print(f"没有种子表：{args.seeds}", file=sys.stderr)
        return 2
    with open(args.seeds, encoding="utf-8-sig") as fh:
        seeds = [r for r in csv.DictReader(fh) if r.get("seed")]
    if args.limit:
        seeds = seeds[:args.limit]
    if not seeds:
        print("种子表是空的。", file=sys.stderr)
        return 2

    print(f"抽样 {len(seeds)} 所（专业代码 {args.major or '未指定'}）\n")
    results = []
    for i, row in enumerate(seeds, 1):
        school, seed = row["school"], row["seed"]
        print(f"[{i}/{len(seeds)}] {school} …", end="", flush=True)
        rec = run_one(school, seed, args.major, row.get("type", ""))
        results.append(rec)
        print(f" 抓取={rec['crawl']} 附件={rec['download'] or 0} 表={rec['parse'] or 0} "
              f"认列={rec['columns'] or '-'} 路径={rec['path'] or '-'} {rec['elapsed_s']}s")

    ok_crawl = sum(1 for r in results if r["crawl"] == "ok")
    ok_assets = sum(1 for r in results if str(r["download"]).isdigit() and int(r["download"] or 0) > 0)
    ok_tables = sum(1 for r in results if str(r["parse"]).isdigit() and int(r["parse"] or 0) > 0)
    ok_cols = sum(1 for r in results if "/" in str(r["columns"]) and
                  int(str(r["columns"]).split("/")[0] or 0) > 0)

    os.makedirs(os.path.dirname(args.out_csv) or ".", exist_ok=True)
    with open(args.out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    n = len(results)
    lines = [
        f"> 抽样日期：{dt.date.today()}｜样本 {n} 所｜专业代码：{args.major or '未指定'}",
        "",
        "# 成功率抽样",
        "",
        "**「认列成功」= 至少一条路径（附件或入口页 HTML 表）能拆出表并认出专业代码列**，",
        "不要求该专业真的存在于这所学校。",
        "",
        "| 环节 | 成功 | 比例 |",
        "|---|---:|---:|",
        f"| 抓取页面 | {ok_crawl} | {ok_crawl / n:.0%} |",
        f"| 下载到附件 | {ok_assets} | {ok_assets / n:.0%} |",
        f"| 解析出表格 | {ok_tables} | {ok_tables / n:.0%} |",
        f"| **认出专业代码列（可出报告）** | {ok_cols} | {ok_cols / n:.0%} |",
        "",
        "| 学校 | 类型 | 抓取 | 附件 | 表 | 认列 | 路径 | 命中 | 耗时 | 备注 |",
        "|---|---|---|---:|---:|---|---|---:|---:|---|",
    ]
    for r in results:
        lines.append(f"| {r['school']} | {r['type'] or '—'} | {r['crawl']} | {r['download'] or '—'} "
                     f"| {r['parse'] or '—'} | {r['columns'] or '—'} | {r['path'] or '—'} "
                     f"| {r['major_hits'] or '—'} | {r['elapsed_s']}s | {r['note'] or ''} |")
    lines += ["", "> 只统计「能不能拿到并解析」；结论是否正确需要人工核对原始表格。"]
    md = "\n".join(lines)
    out_md = os.path.join(DATA_ROOT, "reports", f"{dt.date.today()}-selftest.md")
    os.makedirs(os.path.dirname(out_md), exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as fh:
        fh.write(md)

    print(f"\n成功率：抓取 {ok_crawl}/{n}、附件 {ok_assets}/{n}、表格 {ok_tables}/{n}、认列 {ok_cols}/{n}")
    print(f"→ {os.path.relpath(args.out_csv, DATA_ROOT)}")
    print(f"→ {os.path.relpath(out_md, DATA_ROOT)}")
    return 0 if ok_cols == n else 1


if __name__ == "__main__":
    raise SystemExit(main())

