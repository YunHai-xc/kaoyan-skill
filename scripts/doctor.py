#!/usr/bin/env python3
"""环境自检：装完先跑这个，比跑 check.py 报错再猜要快得多。

检查七件事：
  1. Python 版本（>=3.9）
  2. 依赖包是否都能导入（requests / pandas / pdfplumber / openpyxl / xlrd / lxml）
  3. 终端编码（中文能不能正常输出；Windows 上最常见的坑）
  4. 数据目录可写（KAOYAN_HOME 或 skill 目录）
  5. 模板与台账是否就位（缺了会自动建）
  6. 网络能否访问研招网（只做一次 HEAD，不抓数据）
  7. 中文列名的 HTML 表格能否正确解码（v0.2 修过的那个坑，这里回归一次）

用法：
  python scripts/doctor.py
  python scripts/doctor.py --skip-network
"""
from __future__ import annotations

import argparse
import importlib
import io
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# v0.4.0：默认数据目录放到技能**外面**（同级 kaoyan-data/），技能包保持只读干净。
# 设了 KAOYAN_HOME 就听它的。
DATA_ROOT = os.path.abspath(os.environ.get("KAOYAN_HOME")
                            or os.path.join(os.path.dirname(ROOT), "kaoyan-data"))

OK, BAD, WARN = "✅", "❌", "⚠️"
rows: list[tuple[str, str, str]] = []


def check(name: str, fn) -> bool:
    try:
        detail = fn()
        rows.append((OK, name, detail or ""))
        return True
    except Exception as exc:                        # noqa: BLE001
        msg = str(exc) or type(exc).__name__
        rows.append((BAD, name, msg[:120]))
        return False


def warn_row(name: str, detail: str) -> None:
    rows.append((WARN, name, detail))


# ── 各项检查 ───────────────────────────────────────────────────────────────
def python_version() -> str:
    v = sys.version_info
    if v < (3, 9):
        raise RuntimeError(f"需要 Python 3.9+，当前 {v.major}.{v.minor}.{v.micro}")
    return f"{v.major}.{v.minor}.{v.micro}"


def imports() -> str:
    missing = []
    for mod, pip_name in (("requests", "requests"), ("pandas", "pandas"),
                          ("pdfplumber", "pdfplumber"), ("openpyxl", "openpyxl"),
                          ("xlrd", "xlrd"), ("lxml", "lxml"), ("html5lib", "html5lib")):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pip_name)
    if missing:
        raise RuntimeError(f"缺少：{', '.join(missing)} → pip install {' '.join(missing)}")
    return "7/7"


def encoding() -> str:
    enc = (sys.stdout.encoding or "").lower()
    if os.name == "nt" and "utf" not in enc:
        raise RuntimeError(f"stdout 编码是 {enc}，中文会乱码 → 设 PYTHONIOENCODING=utf-8")
    probe = "专业代码 085404 复试线"
    if probe.encode(enc, "ignore").decode(enc, "ignore") != probe:
        raise RuntimeError(f"当前编码 {enc} 无法表示中文")
    return enc or "unknown"


def data_dir() -> str:
    for sub in ("data", "reports", ".cache"):
        os.makedirs(os.path.join(DATA_ROOT, sub), exist_ok=True)
    probe = os.path.join(DATA_ROOT, ".cache", ".write-test")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("ok")
    os.remove(probe)
    return DATA_ROOT


def templates() -> str:
    sys.path.insert(0, HERE)
    import check as ck
    ck.DATA_ROOT = DATA_ROOT
    ck.REPORTS = os.path.join(DATA_ROOT, "reports")
    ck.DATA = os.path.join(DATA_ROOT, "data")
    ck.CACHE = os.path.join(DATA_ROOT, ".cache")
    ck.SOURCES_CSV = os.path.join(ck.DATA, "sources.csv")
    ck.SCHOOLS_CSV = os.path.join(ck.DATA, "schools.csv")
    ck.ensure_home()
    n = len([f for f in os.listdir(ck.DATA) if f.endswith(".csv")])
    return f"{n} 个 csv 就位"


def network() -> str:
    import requests
    r = requests.head("https://yz.chsi.com.cn/", timeout=12,
                      headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code >= 500:
        raise RuntimeError(f"研招网返回 {r.status_code}")
    return f"HTTP {r.status_code}"


def html_decoding() -> str:
    """回归 v0.2 修过的两个坑：中文列名 + StringIO。"""
    sys.path.insert(0, HERE)
    import extract_tables as et
    html = ("<table><tr><th>学院</th><th>专业代码</th><th>总分</th></tr>"
            "<tr><td>计算机学院</td><td>085404</td><td>325</td></tr></table>")
    tmp = os.path.join(tempfile.mkdtemp(), "t.html")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    tabs = et.read_html_tables(tmp)
    if not tabs or "学院" not in list(tabs[0].columns):
        raise RuntimeError("中文表头没解出来（编码或 StringIO 问题）")
    if str(tabs[0].iloc[0, 1]) not in ("085404", "85404"):
        raise RuntimeError("专业代码列没解出来")
    return "中文表头 + 前导零 OK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-network", action="store_true")
    args = ap.parse_args()

    print(f"数据目录：{DATA_ROOT}\n")
    check("Python 版本", python_version)
    check("依赖包", imports)
    check("终端编码", encoding)
    check("数据目录可写", data_dir)
    check("台账与注册表", templates)
    if args.skip_network:
        warn_row("网络", "已跳过（--skip-network）")
    else:
        check("网络（研招网 HEAD）", network)
    check("HTML 中文解码回归", html_decoding)

    width = max(len(n) for _, n, _ in rows) + 2
    for mark, name, detail in rows:
        print(f"{mark} {name.ljust(width)} {detail}")
    bad = sum(1 for m, _, _ in rows if m == BAD)
    print()
    if bad:
        print(f"{bad} 项未通过。按上面提示处理后重跑。")
        return 1
    print("环境自检通过。下一步：python scripts/pool.py --province 浙江")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

