#!/usr/bin/env python3
"""研招网（chsi）取数：国家线 + 自划线院校链接 + 院校信息。

为什么单独一个脚本：研招网是唯一「跨校统一口径」的官方源，但它的数据入口不统一——
- 专业目录接口 `/zsml/rs/zys.do` 返回「请登录」，抓不了（2026-10-31 实测）
- 历年分数线专题页把数据以 JSON 内嵌在页面里（Vue 应用），不需要登录，可直接解出
- 院校库按名称能查到 schId / 所在地 / 双一流，但不给官网链接

用法：
  python chsi.py --national-line 工学          # 国家线（学硕 + 专硕各一张表）
  python chsi.py --self-score 北京大学          # 34 所自划线院校的逐年复试线链接
  python chsi.py --school 北京                  # 按名称模糊查院校，拿 schId
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
FSX_URL = "https://yz.chsi.com.cn/kyzx/zt/lnfsx.shtml"
SCH_URL = "https://yz.chsi.com.cn/sch/search.do"


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def json_blobs(html: str) -> dict[str, object]:
    """把页面里 `var xxx = {...}` / `window.xxx = [...]` 的内嵌 JSON 全解出来。"""
    out: dict[str, object] = {}
    for m in re.finditer(r"(?:var|window\.)\s*([A-Za-z_$][\w$]*)\s*=\s*(\{|\[)", html):
        start, depth, i = m.end() - 1, 0, m.end() - 1
        while i < len(html):
            if html[i] in "{[":
                depth += 1
            elif html[i] in "}]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        try:
            out[m.group(1)] = json.loads(html[start:i + 1])
        except json.JSONDecodeError:
            continue
    return out


def national_line(sess: requests.Session, kw: str) -> int:
    r = sess.get(FSX_URL, timeout=25)
    r.encoding = "utf-8"
    blobs = json_blobs(r.text)
    rows = []
    for key, label in (("tyhxList", "学术学位"), ("ddhxList", "专业学位")):
        for item in blobs.get(key, []) or []:
            if kw not in str(item.get("name", "")):
                continue
            years = [d.get("year") for d in item.get("dkx", [])]
            gjx = item.get("gjx", {})
            for idx, y in enumerate(years):
                dk = next((d for d in item["dkx"] if d.get("year") == y), {})
                rows.append({
                    "类别": label, "学科门类": item["name"], "年份": y,
                    "A类总分": gjx.get("typea", [None] * 9)[idx] if idx < len(gjx.get("typea", [])) else None,
                    "B类总分": gjx.get("typeb", [None] * 9)[idx] if idx < len(gjx.get("typeb", [])) else None,
                    "A单科100": dk.get("typeaone"), "A单科150": dk.get("typeatwo"),
                    "B单科100": dk.get("typebone"), "B单科150": dk.get("typebtwo"),
                })
    if not rows:
        print(f"没找到「{kw}」的国家线，可试：哲学/经济学/工学/理学/管理学…", file=sys.stderr)
        return 1
    rows.sort(key=lambda x: (x["类别"], x["年份"]), reverse=True)
    print(f"{'类别':<6}{'学科门类':<14}{'年份':<6}{'A总分':>6}{'B总分':>6}{'A单科100':>9}{'A单科150':>9}")
    for x in rows:
        print(f"{x['类别']:<6}{x['学科门类']:<14}{x['年份']:<6}{str(x['A类总分']):>6}{str(x['B类总分']):>6}"
              f"{str(x['A单科100']):>9}{str(x['A单科150']):>9}")
    print("\n口径：A/B 类考生 = 报考地区一区/二区；单科 100=满分100科目线，150=满分>100科目线。")
    print("来源：研招网 历年复试分数线专题 " + FSX_URL)
    return 0


def self_score(sess: requests.Session, school: str) -> int:
    r = sess.get(FSX_URL, timeout=25)
    r.encoding = "utf-8"
    items = json_blobs(r.text).get("zhxList", []) or []
    hit = [x for x in items if school in str(x.get("yxmc", ""))]
    if not hit:
        print(f"「{school}」不在 34 所自划线院校名单里（全国只有这 34 所自划线）。", file=sys.stderr)
        return 1
    for x in hit:
        print(f"== {x['yxmc']}")
        for y in x.get("yearList", []):
            print(f"  {y['year']}  {y['url']}")
    return 0


def school(sess: requests.Session, name: str) -> int:
    """P1-1 修复：院校库的筛选参数是 `yxmc`（v0.1 误用 `dwmc`，导致永远返回北京院校首页）。

    服务端有时忽略参数，所以拿到结果后必须**客户端再过滤一次**，并把「没筛选成功」如实说出来。
    """
    pat = r'/sch/schoolInfo--schId-(\d+)\.dhtml[^>]*>\s*([^<]+?)\s*</a>'
    pairs: list[tuple[str, str]] = []
    used = ""
    for param in ("yxmc", "dwmc"):
        r = sess.get(SCH_URL, params={param: name}, timeout=25)
        r.encoding = "utf-8"
        pairs = re.findall(pat, r.text)
        matched = [(sid, t) for sid, t in pairs if name in t or name[:2] in t]
        if matched:
            used = param
            pairs = matched
            break
    if not used:
        print(f"院校库里没匹配到「{name}」——服务端可能忽略了筛选参数。", file=sys.stderr)
        print("可改用 --school 传更短的校名（如「杭州电子」），或直接在研招网站内搜索。",
              file=sys.stderr)
        return 1
    print(f"（筛选参数：{used}，命中 {len(pairs)} 所）")
    for sid, title in pairs[:15]:
        print(f"  schId={sid:<8} {title}")
    print(f"\n详情页：https://yz.chsi.com.cn/sch/schoolInfo--schId-{pairs[0][0]}.dhtml")
    print("（院校库只给名称/所在地/院校特性，不含专业目录与官网链接）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--national-line", metavar="学科门类")
    ap.add_argument("--self-score", metavar="学校名")
    ap.add_argument("--school", metavar="学校名")
    args = ap.parse_args()
    sess = session()
    if args.national_line:
        return national_line(sess, args.national_line)
    if args.self_score:
        return self_score(sess, args.self_score)
    if args.school:
        return school(sess, args.school)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
