#!/usr/bin/env python3
"""普适抓取器：给一个招生站点，抓页面、挑附件、下载、留清单。

设计原则：**不认识具体学校。** 只做三件跟学校无关的事——
1. 从种子页出发，遍历同一域名下标题/链接命中「招生/简章/目录/复试/录取/推免/调剂」的页面
2. 把页面里所有附件链接（.pdf/.xls/.png 或 download.jsp 这类下载口）挑出来逐个下载
3. 按文件头判断真实类型；拿不到真文件（返回的是「请输入验证码」页面）就标 needs_manual

用法：
  python fetch_school.py --seed https://gr.xidian.edu.cn/info/1074/20804.htm --out out/xidian
  python fetch_school.py --seed https://yz.szu.edu.cn/ --out out/szu --max-pages 120
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from urllib.parse import urljoin, urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ASSET_EXT = (".pdf", ".xls", ".xlsx", ".doc", ".docx", ".png", ".jpg", ".jpeg")
ASSET_HINT = ("download.jsp", "virtual_attach_file", "downloadattach", "wbfileid", "/attach")
KEYWORDS = ("招生", "简章", "目录", "复试", "录取", "推免", "调剂", "分数线", "计划", "名单")
MAX_BYTES = 40 * 1024 * 1024
LINK_RE = re.compile(r"""<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
# v0.2.3：内嵌附件。很多学校把 PDF 塞进 pdfjs 阅读器（pdfsrc="…pdf"），
# 路径裸写在 HTML/JS 里，不在 <a> 标签中 —— 只解析 <a> 会整页漏掉（南邮、南开实测）。
FILE_INLINE_RE = re.compile(r"""["']([^"'<>\s]*?\.(?:pdf|xls|xlsx)(?:\?[^"'<>\s]*)?)["']""", re.I)


DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")


def page_date(html: str) -> str:
    """从页面里抽发布日期（第一个像日期的串）。抽不到返回 unknown —— 不猜。"""
    m = DATE_RE.search(re.sub(r"<[^>]+>", " ", html or ""))
    if not m:
        return "unknown"
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def is_asset(url: str) -> bool:
    low = url.lower()
    if low.split("?")[0].endswith(ASSET_EXT):
        return True
    return any(h in low for h in ASSET_HINT)


def sniff(data: bytes) -> str:
    """按文件头判类型，不信 Content-Type（门户站常年乱报）。"""
    head = data[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"          # 也含 docx
    return "html/other"


def looks_like_captcha(text: str) -> bool:
    return any(k in text for k in ("请输入验证码", "验证码", "统一身份认证", "请登录", "环境异常"))


# 隐私：疑似「含考生姓名的名单」——按标题 + 文件内容双重判断
ROSTER_TITLES = ("拟录取名单", "录取名单", "复试名单", "考生姓名", "待录取名单", "成绩名单")
ROSTER_KEEP = ("统计", "情况", "汇总", "分数", "计划", "目录")
NAME_MARKS = ("姓名".encode("utf-8"), "考生编号".encode("utf-8"), "准考证号".encode("utf-8"))


def looks_like_roster(anchor: str, data: bytes) -> bool:
    """P1-7：只取汇总数字，不落盘含姓名的名单。

    先看标题：命中「拟录取名单/复试名单/考生姓名」这类，且不是「统计/情况/汇总」表 → 判定为名单。
    再看内容：文件头 2MB 内出现「姓名 / 考生编号」也判定为名单（防止标题没写清）。
    """
    title = (anchor or "").strip()
    if any(t in title for t in ROSTER_TITLES) and not any(k in title for k in ROSTER_KEEP):
        return True
    head = data[:2_000_000]
    return any(m in head for m in NAME_MARKS)


def fetch(sess: requests.Session, url: str, timeout: int = 25):
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True)
        return r
    except Exception as exc:                       # noqa: BLE001
        return exc


def crawl(seed: str, max_pages: int, delay: float, sess: requests.Session):
    netloc = urlparse(seed).netloc
    q, seen, pages, assets = deque([seed]), {seed}, [], {}
    while q and len(pages) < max_pages:
        url = q.popleft()
        r = fetch(sess, url)
        if isinstance(r, Exception):
            pages.append({"url": url, "status": "ERR", "note": str(r)[:120]})
            continue
        ctype = r.headers.get("content-type", "")
        if not ctype.lower().startswith("text/html"):
            pages.append({"url": url, "status": r.status_code, "note": f"非HTML：{ctype}"})
            continue
        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
        pages.append({"url": url, "status": r.status_code, "bytes": len(r.content),
                      "title": (re.search(r"<title>(.*?)</title>", html, re.S) or [None, ""])[1].strip()[:80]})
        for href, anchor in LINK_RE.findall(html):
            link = urljoin(url, href.strip())
            if is_asset(link):
                # v0.2.1：附件同时记「所在页面」与「页面发布日期」，供来源四件套使用
                assets.setdefault(link, {
                    "anchor": re.sub(r"<[^>]+>", "", anchor).strip()[:60],
                    "page": url,
                    "date": page_date(html),
                })
                continue
            if urlparse(link).netloc != netloc or link in seen:
                continue
            if re.search(r"\.(zip|rar|7z|mp4|avi)$", link.lower()):
                continue
            if any(k in link or k in anchor for k in KEYWORDS):
                seen.add(link)
                q.append(link)
        # 内嵌附件：把裸写在 HTML 里的 .pdf/.xls/.xlsx 路径也收进来
        for m in FILE_INLINE_RE.finditer(html):
            raw = m.group(1)
            link = urljoin(url, raw)
            if not link.lower().startswith(("http://", "https://")):
                continue
            if link in assets:
                continue
            name = raw.split("/")[-1].split("?")[0][:60] or "内嵌附件"
            assets[link] = {"anchor": name, "page": url, "date": page_date(html)}
        time.sleep(delay)
    return pages, assets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--delay", type=float, default=0.4)
    args = ap.parse_args()

    os.makedirs(os.path.join(args.out, "assets"), exist_ok=True)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

    pages, assets = crawl(args.seed, args.max_pages, args.delay, sess)
    print(f"页面 {len(pages)} 个，候选附件 {len(assets)} 个", file=sys.stderr)

    manifest = []
    skipped_privacy = []
    for url, meta in assets.items():
        anchor = meta.get("anchor", "") if isinstance(meta, dict) else str(meta)
        pub = meta.get("date", "unknown") if isinstance(meta, dict) else "unknown"
        page = meta.get("page", "") if isinstance(meta, dict) else ""
        r = fetch(sess, url, timeout=40)
        if isinstance(r, Exception):
            manifest.append({"url": url, "anchor": anchor, "status": "ERR", "note": str(r)[:120]})
            continue
        data = r.content
        if len(data) > MAX_BYTES:
            manifest.append({"url": url, "anchor": anchor, "status": "TOO_BIG", "bytes": len(data)})
            continue
        kind = sniff(data)
        item = {"url": url, "anchor": anchor, "status": r.status_code, "bytes": len(data),
                "kind": kind, "page": page, "published": pub}
        # 隐私：疑似含考生姓名的名单不落盘（与 check.py 同一套规则）
        if kind != "html/other" and looks_like_roster(anchor, data):
            item["skipped_privacy"] = True
            item["note"] = "疑似含考生姓名，未保存（只记链接）"
            skipped_privacy.append(item)
            manifest.append(item)
            continue
        if kind == "html/other":
            text = data[:4000].decode("utf-8", "ignore")
            item["needs_manual"] = looks_like_captcha(text) or len(data) < 3000
            item["note"] = "返回的不是文件：需验证码/需登录/空页" if item["needs_manual"] else "非文件响应"
        else:
            ext = {"xlsx": "xlsx", "html/other": "bin"}.get(kind, kind)
            name = f"{len(manifest):02d}-{re.sub(r'[^0-9A-Za-z_]', '', anchor)[:24] or 'attach'}.{ext}"
            path = os.path.join(args.out, "assets", name)
            with open(path, "wb") as fh:
                fh.write(data)
            item["file"] = os.path.relpath(path, args.out)
            item["sha256"] = hashlib.sha256(data).hexdigest()[:16]
            if kind == "pdf" and b"/Font" not in data[:200000]:
                item["note"] = "PDF 可能无文字层（扫描件），需 read_image"
        manifest.append(item)
        time.sleep(args.delay)

    payload = {"seed": args.seed, "pages": pages, "assets": manifest}
    with open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    ok = [m for m in manifest if "file" in m]
    manual = [m for m in manifest if m.get("needs_manual")]
    print(f"下载成功 {len(ok)} 个，需人工 {len(manual)} 个，隐私跳过 {len(skipped_privacy)} 个"
          f" → {args.out}/manifest.json")
    for m in ok:
        print(f"  [{m['kind']:4}] {m['file']}  {m['bytes']} B  ← {m['anchor'][:30]}")
    for m in manual:
        print(f"  [人工] {m['status']} {m['url'][:90]}")
    for m in skipped_privacy:
        print(f"  [隐私] 未保存 {m['anchor'][:36]} ← {m['url'][:70]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
