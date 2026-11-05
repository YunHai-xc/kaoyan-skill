#!/usr/bin/env python3
"""一条命令跑完整个流程：学校 + 专业代码 → 体检报告。

v0.2 修复（对应外部评测逐条）：
  P0-1 全新 KAOYAN_HOME 首次运行不再崩溃（数据目录、台账、注册表自动初始化）
  P0-2 专业代码保真：HTML/XLSX 里 085404 被读成 85404 时自动补零
  P0-3 未知不再显示成绿色：全待核 → ⚪ 数据不足；并执行 <20 / <10 小样本规则
  P0-4 报告落地 D×S、来源分层计数、每条来源的 URL / 类型 / 抓取日期
  P1-2 同一专业代码多个招生单位：按学院并列，不静默取第一条
  P1-3 表头变体（一志愿录取人数 / 推免录取人数 / 招生人数（含推免））不再抽错
  P1-5 缓存目录用校名哈希，中文校名不再互相覆盖
  P1-6 自动写台账（幂等），长线状态闭环
  P1-7 疑似含考生姓名的附件不落盘，只记录链接

用法：
  python check.py --school 杭州电子科技大学 --major 085401 \\
      --seed "https://grs.hdu.edu.cn/2026/0929/..." --extra "http://grs.hdu.edu.cn/2026/0324/..."
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 数据根目录：默认与 skill 同目录；设 KAOYAN_HOME 可外置，skill 本体保持只读干净。
# v0.4.0：默认数据目录放到技能**外面**（同级 kaoyan-data/），技能包保持只读干净。
# 设了 KAOYAN_HOME 就听它的。
DATA_ROOT = os.path.abspath(os.environ.get("KAOYAN_HOME")
                            or os.path.join(os.path.dirname(ROOT), "kaoyan-data"))
sys.path.insert(0, HERE)

import fetch_school as fs          # noqa: E402
import extract_tables as et        # noqa: E402

REPORTS = os.path.join(DATA_ROOT, "reports")
DATA = os.path.join(DATA_ROOT, "data")
CACHE = os.path.join(DATA_ROOT, ".cache")
SOURCES_CSV = os.path.join(DATA, "sources.csv")
SCHOOLS_CSV = os.path.join(DATA, "schools.csv")

THRESHOLD_VERSION = "v0.2（经验值，未回测）"
LEDGER_HEADER = ["school", "college", "major_code", "major_name", "degree_type", "study_mode",
                 "enroll_year", "plan_total", "plan_exempt", "plan_special", "quota_exam",
                 "line_school", "line_college", "line_major", "admit_min", "admit_avg",
                 "admit_median", "fc_pass_rate", "retest_ratio", "retest_weight",
                 "subject_codes", "changed_exam", "sources", "note",
                 "campus", "tuition", "dorm_available", "dorm_fee"]

# ── 字段词表 ────────────────────────────────────────────────────────────────
# 顺序即优先级：靠前的字段先认领列名。同一字段只认第一个命中的列。
ALIASES: dict[str, list[str]] = {
    "college": ["招生学院", "学院", "院系所", "院系", "招生单位", "培养单位"],
    "applicants": ["一志愿报考人数", "报考人数", "报名人数", "网报确认", "网报"],
    "online": ["一志愿上线人数", "上线人数", "过国家线人数", "过国家线", "过线人数"],
    "line_pass": ["一志愿上复试线人数", "上复试线人数", "进入复试人数", "实际复试人数", "复试人数"],
    "admit_total": ["录取人数", "录取总数", "合计录取"],
    "exempt": ["其中推免录取人数", "推免录取人数", "实际录取推免", "接收推免人数", "推免人数"],
    "first_choice": ["其中一志愿录取人数", "一志愿录取人数", "实际录取统考", "统考录取人数"],
    "adjust": ["其中调剂录取人数", "调剂录取人数"],
    "max": ["录取最高分", "最高分"],
    "min": ["录取最低分", "最低分"],
    "avg": ["录取平均分", "平均分", "均分"],
    "line": ["复试线", "复试分数线", "专业复试线", "院线", "校线"],
    "line_score": ["总分", "分数线", "复试分数"],
    "national": ["国家线总分", "国家线"],
    "quota_exam": ["统考计划", "可用于统考", "统考名额", "剩余计划", "考试招生计划"],
    "plan": ["简章计划", "拟招生人数", "招生计划", "计划数", "招生人数（不含推免）", "招生人数(不含推免)"],
    "plan_incl": ["招生人数（含推免）", "招生人数(含推免)", "计划招生人数"],
}
# 否决词：出现即说明不是这个字段
EXCLUDE: dict[str, tuple[str, ...]] = {
    "exempt": ("含推免", "不含"),
    "line": ("人数", "上复试线", "比例", "占比"),
    "line_score": ("单科", "人数", "占比"),
    "admit_total": ("率",),
    "avg": ("分差", "差"),
    "plan": ("含推免",),
}
SCORE_FIELDS = {"line", "line_score", "max", "min", "avg", "national"}
SCORE_RANGE = (100, 500)
COUNT_FIELDS = ("applicants", "online", "line_pass", "admit_total", "exempt",
                "first_choice", "adjust", "plan", "plan_incl")
# 可判度的「核心项」（辅助列不计入）
CORE_FIELDS = ("plan", "quota_exam", "exempt", "first_choice", "online",
               "line", "avg", "applicants")
METRIC_OF = {"plan": "计划", "quota_exam": "统考名额", "exempt": "推免",
             "first_choice": "一志愿录取", "online": "一志愿上线",
             "line": "复试线", "avg": "均分", "applicants": "报考"}
FIELD_CN = {"plan": "计划数", "quota_exam": "统考名额", "exempt": "推免",
            "first_choice": "一志愿录取", "online": "上线",
            "line": "复试线", "avg": "均分", "applicants": "报考"}


# ── 基础工具 ───────────────────────────────────────────────────────────────
def norm(s: str) -> str:
    return re.sub(r"\s+|[()（）◎★▲※]", "", str(s))


def num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def norm_code(v) -> str:
    """专业代码保真：处理 '085404' / 85404 / 85404.0 / '085404计算机技术' 等形态。"""
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return ""
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    d = re.sub(r"\D", "", s)
    return d.zfill(6) if d else ""


def year_of(text) -> str | None:
    m = re.search(r"(20\d{2})", str(text or ""))
    return m.group(1) if m else None


def say(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr, flush=True)


def ensure_home() -> None:
    """P0-1：目录与两份台账文件不存在时先建好，避免首次运行在收尾处崩。"""
    for d in (REPORTS, DATA, CACHE):
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(SOURCES_CSV):
        with open(SOURCES_CSV, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(["school", "seed", "note"])
    if not os.path.exists(SCHOOLS_CSV):
        with open(SCHOOLS_CSV, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerow(LEDGER_HEADER)


# ── 抓取 ───────────────────────────────────────────────────────────────────
class FetchError(Exception):
    def __init__(self, code: str, msg: str, hint: str):
        super().__init__(msg)
        self.code, self.msg, self.hint = code, msg, hint


def cache_dir(school: str) -> str:
    """P1-5：中文校名不能再被 re.sub 清成空串，用短哈希保证互不覆盖。"""
    tag = hashlib.sha1(school.encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[^0-9A-Za-z]", "", school)[:8]
    return os.path.join(CACHE, f"check-{safe or 'school'}-{tag}")


def download_assets(seed: str, max_pages: int, out_dir: str) -> tuple[list[dict], list[dict]]:
    sess = fs.requests.Session()
    sess.headers.update({"User-Agent": fs.UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    pages, asset_map = fs.crawl(seed, max_pages, 0.3, sess)
    if not pages:
        raise FetchError("fetch_failed", "一个页面都没抓到",
                         f"检查网络，或用浏览器打开确认能访问：{seed}")
    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    assets, skipped = [], []
    for url, meta in asset_map.items():
        anchor = meta.get("anchor", "") if isinstance(meta, dict) else str(meta)
        pub = meta.get("date", "unknown") if isinstance(meta, dict) else "unknown"
        page = meta.get("page", "") if isinstance(meta, dict) else ""
        r = fs.fetch(sess, url, timeout=40)
        if isinstance(r, Exception):
            warn(f"附件下载失败：{url[:70]}")
            continue
        kind = fs.sniff(r.content)
        if kind not in ("pdf", "xls", "xlsx"):
            continue
        # P1-7 隐私：命中「含考生姓名的名单」类文件时不落盘，只记录链接
        if fs.looks_like_roster(anchor, r.content):
            skipped.append({"anchor": anchor, "url": url})
            continue
        name = f"{len(assets):02d}-{re.sub(r'[^0-9A-Za-z]', '', anchor)[:20] or 'a'}.{kind}"
        with open(os.path.join(out_dir, "assets", name), "wb") as fh:
            fh.write(r.content)
        assets.append({"anchor": anchor, "url": url, "file": name, "kind": kind,
                       "page": page, "published": pub})
    return assets, skipped


# ── 解析与抽取 ─────────────────────────────────────────────────────────────
def pick_cols(headers: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for pass_no in (0, 1):          # 0=精确匹配优先，1=包含匹配
        for idx, raw in enumerate(headers):
            h = norm(raw)
            if not h:
                continue
            for key, words in ALIASES.items():
                if key in out or any(x in h for x in EXCLUDE.get(key, ())):
                    continue
                hit = (h == norm(words[0])) if pass_no == 0 else any(norm(w) in h for w in words)
                if hit:
                    out[key] = idx
    return out


def table_rows(df, major: str) -> list[dict]:
    headers = [str(c) for c in df.columns]
    cols = pick_cols(headers)
    code_idx = et.map_headers(headers).get("major_code")
    if code_idx is None:
        for i, h in enumerate(headers):
            if "代码" in norm(h):
                code_idx = i
                break
    if code_idx is None:
        return []
    rows = []
    for _, row in df.iterrows():
        cells = [str(x) for x in row.tolist()]
        if norm_code(cells[code_idx]) != major:
            continue
        rec = {}
        for key, idx in cols.items():
            v = cells[idx].strip()
            rec[key] = "" if v.lower() == "nan" else v
        for key in SCORE_FIELDS:
            if rec.get(key):
                v = num(rec[key])
                if v is None or not (SCORE_RANGE[0] <= v <= SCORE_RANGE[1]):
                    rec[key] = ""
        for key in COUNT_FIELDS:
            if rec.get(key):
                v = num(rec[key])
                rec[key] = "" if v is None or v < 0 or v > 100000 else str(int(v))
        rec["_cols"] = set(cols)
        rows.append(rec)
    return rows


def collect(assets: list[dict], major: str, out_dir: str) -> tuple[dict, set[str]]:
    """→ ({(年份, 学院): 记录}, 出现过的字段集合)"""
    by_key: dict[tuple[str, str], dict] = {}
    seen_fields: set[str] = set()
    for item in assets:
        path = os.path.join(out_dir, "assets", item["file"])
        if not os.path.exists(path):                 # 也接受「直接给 assets 目录」的调用方
            path = os.path.join(out_dir, item["file"])
        ext = os.path.splitext(path)[1].lower()
        try:
            tables = et.tables_from_pdf(path) if ext == ".pdf" else et.tables_from_excel(path)
        except Exception as exc:                    # noqa: BLE001
            warn(f"解析失败 {os.path.basename(path)}：{type(exc).__name__}")
            continue
        for tname, df in tables:
            if df.empty:
                continue
            df = et.promote_header(df)
            mapping = et.map_headers([str(c) for c in df.columns])
            df = et.normalize(df, mapping)
            for rec in table_rows(df, major):
                y = year_of(item["anchor"]) or year_of(tname) or year_of(item["file"])
                if not y:
                    continue
                college = rec.get("college", "").strip() or "未标学院"
                seen_fields |= rec.get("_cols", set())
                rec.pop("_cols", None)
                by_key.setdefault((y, college), {}).update({k: v for k, v in rec.items() if v})
    return by_key, seen_fields


def collect_html(urls: list[str], major: str) -> tuple[dict, set[str]]:
    """HTML-only 入口：整页就是一张表、没有 PDF/Excel 附件时走这条路（如深大统考计划专页）。

    v0.2.2 补的缺口：此前 check.py 在「没有可解析附件」时直接退出，
    而 README / troubleshooting 都说支持 HTML 表 —— 文档与代码不一致。
    """
    by_key: dict[tuple[str, str], dict] = {}
    seen_fields: set[str] = set()
    for url in urls:
        try:
            tables = [(f"html-t{i}", df.astype(str))
                      for i, df in enumerate(et.read_html_tables(url), 1)]
        except Exception as exc:                    # noqa: BLE001
            warn(f"HTML 表解析失败 {url[:60]}：{type(exc).__name__}")
            continue
        for tname, df in tables:
            if df.empty:
                continue
            df = et.promote_header(df)
            mapping = et.map_headers([str(c) for c in df.columns])
            df = et.normalize(df, mapping)
            # 年份兜底：URL / 表名 / 表头前三行里的「20xx年」都算，实在没有记 unknown
            # （深大的统考计划专页 URL 里没有年份，旧版会把整张表丢掉）
            fy = year_of(url) or year_of(tname)
            if not fy:
                for cell in df.astype(str).head(3).values.flatten().tolist():
                    fy = year_of(cell)
                    if fy:
                        break
            for rec in table_rows(df, major):
                y = fy or "unknown"
                college = rec.get("college", "").strip() or "未标学院"
                seen_fields |= rec.get("_cols", set())
                rec.pop("_cols", None)
                by_key.setdefault((y, college), {}).update(
                    {k: v for k, v in rec.items() if v})
    return by_key, seen_fields


def parse_extra(url: str, major: str) -> dict[str, dict]:
    """补充页（复试线常单独发一张 HTML 表）→ {年份: {line, note}}。

    多个招生单位各自划线时全部并列记入口径，主值取最高（最保守），绝不静默取最后一行。
    """
    year = year_of(url)
    out: dict[str, dict] = {}
    try:
        tabs = et.read_html_tables(url)          # 统一编码处理，避免中文列名乱码
    except Exception as exc:                    # noqa: BLE001
        warn(f"补充页解析失败 {url[:60]}：{type(exc).__name__}")
        return out
    scores: list[tuple[str, int]] = []
    for df in tabs:
        df = df.astype(str)
        df = et.promote_header(df)
        mapping = et.map_headers([str(c) for c in df.columns])
        df = et.normalize(df, mapping)
        for rec in table_rows(df, major):
            v = num(rec.get("line_score", ""))
            if v and SCORE_RANGE[0] <= v <= SCORE_RANGE[1]:
                scores.append((rec.get("college", "").strip(), int(v)))
    if year and scores:
        scores.sort(key=lambda x: -x[1])
        out[year] = {"line": str(scores[0][1]),
                     "line_note": "；".join(f"{v}（{c or '未标学院'}）" for c, v in scores)}
    return out


# ── 指标与判定 ─────────────────────────────────────────────────────────────
def metrics(rec: dict) -> dict:
    ex = num(rec.get("exempt"))
    first = num(rec.get("first_choice"))
    admit = num(rec.get("admit_total"))
    online = num(rec.get("online"))
    if admit is None and ex is not None and first is not None:
        admit = ex + first
    m = {
        "录取": admit, "推免": ex, "一志愿录取": first, "一志愿上线": online,
        "报考": num(rec.get("applicants")), "复试线": num(rec.get("line")),
        "均分": num(rec.get("avg")), "最低": num(rec.get("min")),
        "国家线": num(rec.get("national")), "计划": num(rec.get("plan")),
        "统考名额": num(rec.get("quota_exam")),
        "复试人数": num(rec.get("line_pass")),
    }
    m["推免占比"] = round(ex / admit, 3) if admit and ex is not None else None
    m["一志愿录取率"] = round(first / online, 3) if online and first is not None else None
    m["上线比"] = round(online / first, 1) if online and first else None
    m["分差"] = round(m["均分"] - m["复试线"], 1) if m["均分"] and m["复试线"] else None
    m["复试线口径"] = rec.get("_line_note", "")
    return m


def signals(rows: dict[str, dict]) -> tuple[list[tuple[str, str, str]], dict]:
    """R1~R6 三态判定。返回 ([(编号, ✓/✗/待核, 依据)], 统计)。"""
    years = sorted(rows)
    out: list[tuple[str, str, str]] = []
    if not years:
        return out, {"hit": 0, "miss": 0, "unknown": 0}

    gaps = [(y, rows[y]["复试线"] - rows[y]["国家线"]) for y in years
            if rows[y]["复试线"] and rows[y]["国家线"]]
    if len(gaps) >= 2:
        out.append(("R1 洼地效应", "✓" if all(g <= 10 for _, g in gaps[-2:]) else "✗",
                    f"复试线−国家线 = {', '.join(f'{g:+.0f}' for _, g in gaps)}（阈值 ≤10）"))
    else:
        out.append(("R1 洼地效应", "待核", "国家线或复试线不足两年"))

    fc = [(y, rows[y]["一志愿录取"]) for y in years if rows[y]["一志愿录取"]]
    if len(fc) >= 2:
        drops = [(fc[i][0], (fc[i][1] - fc[i - 1][1]) / fc[i - 1][1]) for i in range(1, len(fc))]
        worst = min(drops, key=lambda x: x[1])
        out.append(("R2 持续缩招", "✓" if worst[1] <= -0.20 else "✗",
                    f"一志愿录取 {'→'.join(str(int(v)) for _, v in fc)}；最大同比变化 "
                    f"{worst[1]:+.1%}（{worst[0]}，阈值 ≤−20%）"))
    else:
        out.append(("R2 持续缩招", "待核", "统考录取不足两年"))

    ap = [(y, rows[y]["报考"]) for y in years if rows[y]["报考"]]
    if len(ap) >= 2 and ap[0][1]:
        growth = (ap[-1][1] - ap[0][1]) / ap[0][1]
        out.append(("R3 热度飙升", "✓" if growth >= 0.30 else "✗",
                    f"报考 {'→'.join(str(int(v)) for _, v in ap)}，累计 {growth:+.0%}（阈值 ≥30%）"))
    else:
        out.append(("R3 热度飙升", "待核", "缺报考人数"))

    rates = [(y, rows[y]["一志愿录取率"]) for y in years if rows[y]["一志愿录取率"]]
    if rates:
        last = rates[-1][1]
        out.append(("R4 一志愿录取率", "✓" if last < 0.20 else ("待核" if last < 0.35 else "✗"),
                    f"{'→'.join(f'{r:.0%}' for _, r in rates)}（<20% 命中，20~35% 待核）"))
    else:
        out.append(("R4 一志愿录取率", "待核", "缺上线人数"))

    sh = [(y, rows[y]["推免占比"]) for y in years if rows[y]["推免占比"] is not None]
    if len(sh) >= 2:
        delta = sh[-1][1] - sh[0][1]
        out.append(("R5 推免挤压", "✓" if (delta >= 0.10 or sh[-1][1] >= 0.50) else "✗",
                    f"推免占比 {'→'.join(f'{r:.0%}' for _, r in sh)}，累计 {delta:+.1%}pp"
                    f"（阈值 ≥10pp 或末值 ≥50%）"))
    else:
        out.append(("R5 推免挤压", "待核", "推免占比不足两年"))

    ds = [(y, rows[y]["分差"]) for y in years if rows[y]["分差"] is not None]
    if ds:
        out.append(("R6 复试线虚低", "✓" if any(d >= 40 for _, d in ds) else "✗",
                    f"均分−复试线 = {', '.join(f'{d:+.1f}' for _, d in ds)}（阈值 ≥40）"))
    else:
        out.append(("R6 复试线虚低", "待核", "缺均分或复试线"))

    stat = {"hit": sum(1 for _, f, _ in out if f == "✓"),
            "miss": sum(1 for _, f, _ in out if f == "✗"),
            "unknown": sum(1 for _, f, _ in out if f == "待核")}

    # R7 / R8 只作信息行，不参与命中计数：
    # R8 是 R1+R6 的合成结果（重复计分会把同一件事算两次）；R7 必须人工查专业目录。
    r1 = next((f for n, f, _ in out if n.startswith("R1")), "待核")
    r6 = next((f for n, f, _ in out if n.startswith("R6")), "待核")
    if r1 == "✓" and r6 == "✓":
        r8 = "✓（由 R1+R6 推出，不重复计入命中数）"
    elif "待核" in (r1, r6):
        r8 = "待核（依赖 R1 / R6）"
    else:
        r8 = "✗"
    out.append(("R7 改考", "待核（需人工）", "查专业目录：专业课代码是否变化、是否改考 408、参考书是否大改"))
    out.append(("R8 双坑组合", r8, "R1 与 R6 同时命中时为真——信息行，不计入命中统计"))
    return out, stat


def grade(stat: dict, small_sample: float | None) -> tuple[str, str]:
    """P0-3：全待核 → ⚪ 数据不足，绝不显示绿色。小样本按 <20 / <10 修正。"""
    hit, unknown = stat["hit"], stat["unknown"]
    if hit >= 3:
        level, emoji = "高", "🔴"
    elif hit == 2:
        level, emoji = "中", "🟠"
    elif hit == 1:
        level, emoji = "低-中", "🟡"
    elif unknown:
        level, emoji = "未知（数据不足）", "⚪"
    else:
        level, emoji = "未见明显风险", "🟢"

    note = []
    if small_sample:
        if small_sample < 10:
            if emoji in ("🟢", "⚪", "🟡"):
                emoji, level = "🟠", "中"
            note.append(f"统考名额仅 {int(small_sample)} 人（<10）→ 风险等级至少「中」")
        elif small_sample < 20:
            if emoji == "🟢":
                emoji, level = "🟡", "低-中"
            note.append(f"统考名额仅 {int(small_sample)} 人（<20）→ 小样本，结论下调一档")
    if unknown:
        note.append(f"{unknown} 项因缺数据判不了：既不计入命中，也不等于「没风险」")
    return f"{emoji} {level}", "；".join(note)


def coverage(per_college: dict[str, dict[str, dict]], seen: set[str]):
    """P1(v0.2.1)：分母**固定**为「七项 × 年数 × 招生单位数」。

    旧版把「整列不存在的字段」剔出分母，等于把缺失洗成 n/a，D 被系统性抬高
    （杭电报告里 `计划`、`复试线` 明明是空的，D 却是 0.93）。现在：
      · 缺格一律扣分，不许剔出分母
      · 但要分两类记录：`unknown`（有这一列但没数值）与 `n/a`（该校整份材料里没这一列）
        —— 报告里分开写，读者才知道是「没查到」还是「学校不公布」
    """
    years = sorted({y for c in per_college.values() for y in c})[-3:]
    fields = list(CORE_FIELDS)
    total = len(fields) * len(years) * max(1, len(per_college))
    filled, holes, na_holes = 0, [], []
    for college, per_year in per_college.items():
        for y in years:
            m = per_year.get(y) or {}
            for f in fields:
                if m.get(METRIC_OF[f]) not in (None, ""):
                    filled += 1
                    continue
                tag = f"{y}.{college}.{FIELD_CN[f]}"
                if f in seen:
                    holes.append(tag)          # 表里有这一列，但该校没填/没抓到
                else:
                    na_holes.append(tag)       # 整份材料都没有这一列
    return (round(filled / total, 2) if total else 0.0), holes, na_holes, filled, total


def source_grade(url: str, seed: str) -> str:
    """L1 同校官网 / L2 其他 .edu.cn / L3 第三方。"""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    seed_host = re.sub(r"^https?://", "", seed).split("/")[0].lower()

    def core(h: str) -> str:
        parts = h.split(".")
        return ".".join(parts[-3:]) if len(parts) >= 3 else h

    if core(host) == core(seed_host):
        return "L1"
    if host.endswith(".edu.cn"):
        return "L2"
    return "L3"


def s_factor(grades: list[str]) -> tuple[float, dict[str, int]]:
    cnt = {g: grades.count(g) for g in ("L1", "L2", "L3", "L4")}
    if cnt["L4"]:
        return 0.6, cnt
    if cnt["L3"]:
        return 0.85, cnt
    return 1.0, cnt


def confidence(d: float, s: float, l1_count: int) -> tuple[float, str, str]:
    """P(-)v0.2.1：落地「L1 来源不足 2 条 → 最高只能给『中』」的硬门槛。

    文档里写了但代码没执行，评测用一个 L1=1 / L2=1 的样本打穿了它。
    """
    score = round(d * s, 2)
    conf = "高" if score >= 0.70 else "中" if score >= 0.45 else "低"
    gate = ""
    if l1_count < 2 and conf == "高":
        conf = "中"
        gate = f"L1 来源仅 {l1_count} 条（<2）→ 置信度封顶「中」（硬门槛，不随分数上浮）"
    elif l1_count < 2:
        gate = f"L1 来源仅 {l1_count} 条（<2）"
    return score, conf, gate


# ── 报告 ───────────────────────────────────────────────────────────────────
def fmt(v, kind: str = "") -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v:.0%}"
    if kind == "x":
        return f"{v}×"
    if kind == "gap":
        return f"{v:+.1f}"
    if isinstance(v, float):
        return f"{v:.0f}" if abs(v - round(v)) < 1e-9 else f"{v}"
    return str(v)


def render(school: str, major: str, per_college: dict, sig: list, stat: dict,
           level: str, level_note: str, d: float, s: float, score: float, conf: str,
           holes: list[str], na_holes: list[str], filled: int, total: int,
           src_rows: list[dict], seed: str, skipped: list[dict],
           main_college: str, gate: str = "") -> str:
    years = sorted({y for c in per_college.values() for y in c})[-3:]
    grades = [r["grade"] for r in src_rows]
    _, cnt = s_factor(grades)
    L: list[str] = []
    L.append(f"> 对象：{school} / {major}｜招生单位：{len(per_college)} 个（主：{main_college}）")
    L.append(f"> 数据年份：{'/'.join(years)}（录取年＝入学年）｜生成：{dt.date.today()}")
    L.append(f"> 数据来源：{len(src_rows)} 条（L1 {cnt['L1']} / L2 {cnt['L2']} / L3 {cnt['L3']}）")
    L.append(f"> 入口页：{seed}")
    L.append(f"> 可判度：D {d:.2f} × S {s:.2f} = {score:.2f} → **{conf}**"
             f"｜覆盖 {filled}/{total} 格（分母固定为七项 × 年数 × 招生单位数）")
    if gate:
        L.append(f"> ⚠️ {gate}")
    if holes:
        short = "、".join(holes[:6]) + ("…" if len(holes) > 6 else "")
        L.append(f"> 缺格（有该列但没数值，{len(holes)} 个）：{short}")
    if na_holes:
        short = "、".join(na_holes[:6]) + ("…" if len(na_holes) > 6 else "")
        L.append(f"> n/a（整份材料里没有该列，{len(na_holes)} 个，仍计入分母）：{short}")
    if skipped:
        L.append(f"> 隐私跳过：{len(skipped)} 个疑似含考生姓名的文件未下载（见文末）")
    L.append("")
    L.append(f"# {school} · {major} 体检报告")
    L.append("")

    for college, per_year in per_college.items():
        ys = sorted(per_year)
        L.append(f"## 数据：{college}")
        L.append("")
        L.append("| 项 | " + " | ".join(ys) + " |")
        L.append("|---|" + "---:|" * len(ys))
        for k in ("计划", "统考名额", "报考", "一志愿上线", "一志愿录取", "推免", "录取",
                  "复试线", "均分", "最低", "国家线"):
            L.append(f"| {k} | " + " | ".join(fmt(per_year[y].get(k)) for y in ys) + " |")
        for k, kind in (("推免占比", "pct"), ("一志愿录取率", "pct"),
                        ("上线比", "x"), ("分差", "gap")):
            L.append(f"| {k} | " + " | ".join(fmt(per_year[y].get(k), kind) for y in ys) + " |")
        notes = [(y, per_year[y]["复试线口径"]) for y in ys if per_year[y].get("复试线口径")]
        if notes:
            L.append("")
            for y, n in notes:
                L.append(f"> 复试线口径（{y}）：{n} —— 多个招生单位各自划线，主值取最高（最保守）。")
        L.append("")

    L.append(f"## 风险信号（针对主招生单位：{main_college}）")
    L.append("")
    L.append("| 编号 | 判定 | 依据 |")
    L.append("|---|---|---|")
    for name, flag, why in sig:
        L.append(f"| {name} | {flag} | {why} |")
    L.append("")
    L.append(f"**命中 {stat['hit']} 项｜未命中 {stat['miss']} 项｜待核 {stat['unknown']} 项 → {level}**")
    if level_note:
        L.append("")
        L.append(f"> {level_note}")
    L.append("")
    L.append("> **待核不是「没风险」。** 数据缺了就是还不知道，按 `school.md` 的规矩不许补 0，"
             "也不许显示成绿色。")
    L.append("")

    L.append("## 钱和住")
    L.append("")
    L.append("脚本不采集校区/学费/住宿（不在录取情况表里）。按 `references/life.md` 查："
             "简章「学费、住宿和校区」条款 + 校区/学费公示附表。")
    L.append("")
    L.append("## 还没做的")
    L.append("")
    L.append("- 估分 → Δ → 冲稳保：要考生侧给数，脚本不代猜")
    L.append("- 初试科目、复试规则：需读专业目录与复试办法，未自动化")
    L.append(f"- 阈值版本：{THRESHOLD_VERSION}；命中数只代表「信号亮了」，不代表录取概率")
    L.append("")
    L.append("## 来源清单")
    L.append("")
    L.append("| 等级 | 类型 | 标题 | URL | 发布日期 | 访问日期 |")
    L.append("|---|---|---|---|---|---|")
    for r in src_rows:
        L.append(f"| {r['grade']} | {r['kind']} | {r['title'][:28]} | {r['url']} "
                 f"| {r.get('published') or 'unknown'} | {r.get('accessed') or r.get('date', '')} |")
    if skipped:
        L.append("")
        L.append("### 未下载（疑似含个人信息）")
        L.append("")
        for r in skipped:
            L.append(f"- {r['anchor'][:40] or '（无标题）'} —— {r['url']}")
        L.append("")
        L.append("> 本工具只取汇总数字（人数/分数），不保存含考生姓名的名单。需要时请自行打开链接查看。")
    L.append("")
    L.append("---")
    L.append("> 数字来自上述附件解析，未做人工读数核对；PNG/扫描件不在自动解析范围。")
    return "\n".join(L)


# ── 台账与注册表 ───────────────────────────────────────────────────────────
def append_ledger(school: str, major: str, per_college: dict, src_url: str,
                  pub_date: str = "unknown") -> int:
    ensure_home()
    today = str(dt.date.today())
    with open(SCHOOLS_CSV, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
        header = list(rows[0].keys()) if rows else LEDGER_HEADER
    seen = {(r.get("school"), r.get("college"), r.get("major_code"), r.get("enroll_year")) for r in rows}
    added = 0
    for college, per_year in per_college.items():
        for y, m in sorted(per_year.items()):
            if (school, college, major, y) in seen:
                continue
            rec = {h: "unknown" for h in header}
            rec.update({
                "school": school, "college": college, "major_code": major,
                "major_name": "unknown", "degree_type": "unknown", "study_mode": "unknown",
                "enroll_year": y,
                "plan_total": fmt(m.get("计划")) if m.get("计划") else "unknown",
                "plan_exempt": fmt(m.get("推免")) if m.get("推免") is not None else "unknown",
                "line_major": fmt(m.get("复试线")) if m.get("复试线") else "unknown",
                "admit_min": fmt(m.get("最低")) if m.get("最低") else "unknown",
                "admit_avg": fmt(m.get("均分")) if m.get("均分") else "unknown",
                "fc_pass_rate": (fmt(m.get("上线比")) if m.get("上线比") is not None else "unknown"),
                "sources": (f"学校官网|{src_url}|{pub_date}|{today}" if src_url
                            else f"unknown|unknown|unknown|{today}"),
                "note": f"check.py 自动写入；fc_pass_rate=一志愿上线率(上线÷统考录取，可>1)；"
                        f"一志愿录取率={m.get('一志愿录取率')}；推免占比={m.get('推免占比')}；"
                        f"复试线口径={m.get('复试线口径') or 'unknown'}",
            })
            rows.append(rec)
            added += 1
    if added:
        with open(SCHOOLS_CSV, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=header)
            w.writeheader()
            w.writerows(rows)
    return added


def remember_seed(school: str, seed: str) -> None:
    ensure_home()
    with open(SOURCES_CSV, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if any(r.get("school") == school for r in rows):
        return
    rows.append({"school": school, "seed": seed, "note": "首次运行自动记录"})
    with open(SOURCES_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["school", "seed", "note"])
        w.writeheader()
        w.writerows(rows)


def seed_candidates() -> list[tuple[str, str]]:
    """用户注册表优先：同名时内置表不再覆盖用户填的入口页。"""
    merged: dict[str, str] = {}
    for path in (SOURCES_CSV, os.path.join(ROOT, "data", "seeds.builtin.csv")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("school") or "").strip()
                seed = row.get("seed")
                if name and seed and name not in merged:
                    merged[name] = seed
    return list(merged.items())


def resolve_seed(school: str) -> tuple[str | None, str]:
    """v0.2.2：精确匹配优先，再多候选时报歧义，绝不因为子串命中就随便挑一个。

    旧版用子串匹配：查「杭州电子科技大学」可能命中
    「杭州电子科技大学信息工程学院」（独立学院，入口页完全不同）。
    """
    cands = list(dict.fromkeys(seed_candidates()))   # 用户注册表与内置表可能重复，先去重
    exact = [c for c in cands if c[0] == school]
    exact_seeds = {c[1] for c in exact}
    if len(exact_seeds) == 1:
        return exact_seeds.pop(), ""
    if len(exact_seeds) > 1:
        return None, (f"「{school}」在注册表里有 {len(exact_seeds)} 个不同入口页，"
                      f"请用 --seed 明确指定，或清理 data/sources.csv")
    loose = [c for c in cands if school in c[0] or c[0] in school]
    loose_seeds = {c[1] for c in loose}
    if len(loose_seeds) == 1:
        return loose_seeds.pop(), f"（按「{loose[0][0]}」匹配）"
    if len(loose_seeds) > 1:
        names = "、".join(list(dict.fromkeys(n for n, _ in loose))[:6])
        return None, (f"「{school}」匹配到 {len(loose_seeds)} 个入口页：{names}。\n"
                      f"请写完整校名，或用 --seed 直接指定。")
    return None, ""


# ── 主流程 ─────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="考研择校体检：学校 + 专业代码 → 报告")
    ap.add_argument("--school", required=True)
    ap.add_argument("--major", required=True, help="六位专业代码，如 085401")
    ap.add_argument("--seed", help="招生站点入口页；首次必填，之后自动记住")
    ap.add_argument("--extra", action="append", default=[], help="补充页（复试线等），可重复")
    ap.add_argument("--max-pages", type=int, default=8)
    ap.add_argument("--no-ledger", action="store_true", help="不写台账")
    args = ap.parse_args()

    ensure_home()
    major = norm_code(args.major)
    if len(major) != 6:
        say(f"专业代码看起来不对：{args.major}。需要六位数字，例如 085401。")
        return 2

    seed, seed_note = (args.seed, "") if args.seed else resolve_seed(args.school)
    if seed_note:
        say(seed_note)
    if not seed:
        say(f"没有「{args.school}」的入口页，第一次需要你给一个。")
        say("怎么找：搜「<学校> 研究生招生 录取情况」或「<学校> 硕士招生专业目录」，")
        say("       打开后把地址栏的网址整个复制过来，作为 --seed。")
        say(f"       检索式：{args.school} 研究生招生 硕士 录取情况 filetype:pdf")
        say("给过一次会记进 data/sources.csv，之后只写 --school 即可。")
        return 2

    out_dir = cache_dir(args.school)
    say(f"[1/5] 抓取：{seed}")
    try:
        assets, skipped = download_assets(seed, args.max_pages, out_dir)
    except FetchError as exc:
        say(f"抓取失败：{exc.msg}")
        say(f"建议：{exc.hint}")
        return 3
    say(f"      可解析附件 {len(assets)} 个" + (f"，隐私跳过 {len(skipped)} 个" if skipped else ""))

    # v0.2.2：没有附件时不再直接退出，改走 HTML-only 路径（整页就是一张表的情况，如深大）
    html_mode = not assets
    if html_mode:
        say("      没有 PDF/Excel 附件，按 HTML 表解析入口页（含 --extra）…")
        by_key, seen = collect_html([seed] + list(args.extra), major)
        if not by_key:
            say("这页既没有可解析附件，也没有能认出专业代码的 HTML 表。可能原因：")
            say("  · 数据以图片发布 → 需人工读数（见 references/pipeline.md「障碍处置」第 1 条）")
            say("  · 这页不是「录取情况 / 招生计划」类页面 → 换个入口页")
            say("  · 表里没有专业代码列 → 用 --major 换一个代码试试")
            return 3
    else:
        by_key, seen = collect(assets, major, out_dir)

    say(f"[2/5] 解析 + 抽取（专业代码 {major}）")
    for u in args.extra:
        for y, rec in parse_extra(u, major).items():
            hit = False
            for (yy, college) in list(by_key):
                if yy == y:
                    by_key[(yy, college)].setdefault("line", rec["line"])
                    by_key[(yy, college)].setdefault("_line_note", rec.get("line_note", ""))
                    hit = True
            if hit:
                say(f"      补充页 {y}：复试线 {rec['line']} ← {rec.get('line_note', '')}")
    if not by_key:
        say(f"没找到专业代码 {major} 的任何一行。")
        # 区分「附件里根本没有这个代码」与「有，但表格结构拆不开」——两者结论完全不同
        raw_hit = []
        for a in assets:
            path = os.path.join(out_dir, "assets", a["file"])
            if not path.lower().endswith(".pdf"):
                continue
            try:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    for pno, page in enumerate(pdf.pages[:40], 1):
                        if major in (page.extract_text() or ""):
                            raw_hit.append(f"{a['anchor'][:26]} 第 {pno} 页")
                            break
            except Exception:                       # noqa: BLE001
                continue
        if raw_hit:
            say(f"  但附件正文里**出现了 {major}**：{'；'.join(raw_hit[:3])}")
            say("  → 属于「有数据但表格结构拆不开」（常见于跨页合并单元格的目录 PDF）。")
            say(f"     可以人工打开：{out_dir}/assets")
            return 5
        say("可能原因：")
        say("  · 该校该专业今年不招，或代码不对（085404 与 085401 是两个方向）")
        say("  · 表格把代码写成了「085404计算机技术」并被截断")
        say(f"  · 先人工看一眼：{out_dir}/assets")
        return 4

    per_college: dict[str, dict[str, dict]] = {}
    for (y, college), rec in by_key.items():
        per_college.setdefault(college, {})[y] = metrics(rec)
    main_college = max(per_college, key=lambda c: max(
        (m.get("一志愿录取") or 0) for m in per_college[c].values()))

    say("[3/5] 判定（R1~R6 三态）")
    sig, stat = signals(per_college[main_college])
    latest = max(per_college[main_college])
    small = per_college[main_college][latest].get("一志愿录取")
    level, level_note = grade(stat, small)

    say("[4/5] 可判度与来源审计")
    d, holes, na_holes, filled, total = coverage(per_college, seen)
    today = str(dt.date.today())
    pub_dates = [a.get("published") for a in assets if a.get("published") not in (None, "", "unknown")]
    seed_pub = max(pub_dates) if pub_dates else "unknown"
    src_rows = [{"grade": source_grade(seed, seed),
                 "kind": "html（入口页表格）" if html_mode else "入口页",
                 "title": "招生目录 / 录取情况", "url": seed,
                 "published": seed_pub, "accessed": today, "date": today}]
    for a in assets:
        src_rows.append({"grade": source_grade(a["url"], seed), "kind": a["kind"],
                         "title": a["anchor"], "url": a["url"],
                         "published": a.get("published") or "unknown",
                         "accessed": today, "date": today})
    for u in args.extra:
        src_rows.append({"grade": source_grade(u, seed), "kind": "html",
                         "title": "补充页（复试线）", "url": u,
                         "published": "unknown", "accessed": today, "date": today})
    s, cnt = s_factor([r["grade"] for r in src_rows])
    score, conf, gate = confidence(d, s, cnt["L1"])

    say("[5/5] 出报告")
    md = render(args.school, major, per_college, sig, stat, level, level_note,
                d, s, score, conf, holes, na_holes, filled, total,
                src_rows, seed, skipped, main_college, gate)
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, f"{dt.date.today()}-{args.school}-{major}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(md)
    remember_seed(args.school, seed)
    added = 0 if args.no_ledger else append_ledger(args.school, major, per_college, seed, seed_pub)
    say(f"      → {os.path.relpath(path, DATA_ROOT)}")
    say(f"      可判度 {d:.2f}×{s:.2f}={score:.2f}（{conf}）｜{level}｜台账新增 {added} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

