#!/usr/bin/env python3
"""建招生单位池：按省市从研招网院校库抓「有哪些学校可选」。

为什么需要它：择校的第一步不是"比较学校"，是"知道有哪些学校存在"。
研招网院校库支持 `ssdm` 省市筛选（实测：ssdm=33 → 浙江大学、杭电、浙工大…），
分页参数是 `start`（步长 20），这就是一个可枚举的全国候选池。

用法：
  python pool.py                      # 抓全部 31 个省，存 data/pool.csv（约 20~40 秒）
  python pool.py --province 浙江 广东   # 只抓指定省
  python pool.py --list               # 只看已缓存的池子

注意：这个池子只含「学校 → 省份 → 是否双一流」，**不含专业**。
某校有没有你的专业，要靠 check.py 去查它的招生目录。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://yz.chsi.com.cn/sch/search.do"
DATA_ROOT = os.path.abspath(os.environ.get("KAOYAN_HOME")
                            or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
POOL = os.path.join(DATA_ROOT, "data", "pool.csv")
FIELDS = ["province", "school", "sch_id", "tag"]

PROVINCES = {
    "11": "北京", "12": "天津", "13": "河北", "14": "山西", "15": "内蒙古",
    "21": "辽宁", "22": "吉林", "23": "黑龙江", "31": "上海", "32": "江苏",
    "33": "浙江", "34": "安徽", "35": "福建", "36": "江西", "37": "山东",
    "41": "河南", "42": "湖北", "43": "湖南", "44": "广东", "45": "广西",
    "46": "海南", "50": "重庆", "51": "四川", "52": "贵州", "53": "云南",
    "54": "西藏", "61": "陕西", "62": "甘肃", "63": "青海", "64": "宁夏", "65": "新疆",
}
NAME_RE = re.compile(r'/sch/schoolInfo--schId-(\d+)\.dhtml[^>]*>\s*([^<]+?)\s*</a>')
TAG_RE = re.compile(r'sch-tag[^>]*>\s*([^<]+?)\s*<')


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def fetch_province(sess: requests.Session, code: str, delay: float = 0.3) -> list[dict]:
    """按 start 步长翻页，直到某页没有院校为止。"""
    out: list[dict] = []
    start = 0
    while start <= 400:
        url = f"{BASE}?ssdm={code}&start={start}"
        try:
            r = sess.get(url, timeout=25)
            r.encoding = "utf-8"
        except Exception as exc:                       # noqa: BLE001
            print(f"  ! {code} start={start} 请求失败：{exc}", file=sys.stderr)
            break
        html = r.text
        names = NAME_RE.findall(html)
        if not names:
            break
        # 标签跟在每个名字后面不远，按出现位置配对
        tags = {}
        for m in TAG_RE.finditer(html):
            anchor = html.rfind("schId-", 0, m.start())
            sid = re.search(r"schId-(\d+)", html[anchor:anchor + 60])
            if sid:
                tags.setdefault(sid.group(1), m.group(1).strip())
        for sid, name in names:
            out.append({
                "province": PROVINCES.get(code, code),
                "school": name.strip(),
                "sch_id": sid,
                "tag": tags.get(sid, ""),
            })
        start += 20
        time.sleep(delay)
    return out


def load_pool() -> list[dict]:
    if not os.path.exists(POOL):
        return []
    with open(POOL, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def save_pool(rows: list[dict]) -> None:
    seen, uniq = set(), []
    for r in rows:
        key = (r["province"], r["school"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda r: (r["province"], r["school"]))
    os.makedirs(os.path.dirname(POOL), exist_ok=True)
    with open(POOL, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(uniq)
    print(f"写入 {os.path.relpath(POOL, DATA_ROOT)}：{len(uniq)} 所")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--province", nargs="*", help="省份中文名，留空=全部")
    ap.add_argument("--list", action="store_true", help="只列已缓存池子的统计")
    ap.add_argument("--delay", type=float, default=0.3)
    args = ap.parse_args()

    if args.list:
        rows = load_pool()
        if not rows:
            print("池子还是空的，先跑一次不带 --list 的。")
            return 1
        by_prov: dict[str, int] = {}
        for r in rows:
            by_prov[r["province"]] = by_prov.get(r["province"], 0) + 1
        print(f"共 {len(rows)} 所，覆盖 {len(by_prov)} 个省市")
        for prov, n in sorted(by_prov.items(), key=lambda x: -x[1]):
            print(f"  {prov:<6} {n:>3} 所")
        double = sum(1 for r in rows if "双一流" in r.get("tag", ""))
        print(f"其中标注「双一流」{double} 所")
        return 0

    targets = {c: n for c, n in PROVINCES.items()
               if not args.province or n in args.province}
    if not targets:
        print(f"没匹配到省份。可选：{'、'.join(PROVINCES.values())}", file=sys.stderr)
        return 2

    sess = session()
    rows = load_pool() if args.province else []      # 指定省份时保留其它省已有数据
    for code, name in targets.items():
        got = fetch_province(sess, code, args.delay)
        rows = [r for r in rows if r["province"] != name] + got
        print(f"{name}({code})：{len(got)} 所")
    save_pool(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
