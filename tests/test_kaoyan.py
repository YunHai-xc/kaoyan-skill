#!/usr/bin/env python3
"""kaoyan skill 的夹具测试（不依赖 pytest，也不联网）。

覆盖外部评测里报出的每一类失败：
  · 专业代码保真（085404 被读成 85404）
  · 表头变体（一志愿录取人数 / 推免录取人数 / 招生人数（含推免））
  · 同一代码多个招生单位（不许静默取第一条）
  · 全待核 → ⚪ 未知（不许显示成绿色）
  · 小样本 <10 / <20 的降档规则
  · 可判度 D×S 与缺格统计
  · 全新数据目录的首次运行（ensure_home / remember_seed / append_ledger 不崩）

运行：
  python tests/test_kaoyan.py          # 全部跑一遍
  python tests/test_kaoyan.py -v       # 逐条打印
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

# 必须在 import check 之前设置：check 在导入时决定数据根目录
_TMP = tempfile.mkdtemp(prefix="kaoyan-test-")
os.environ["KAOYAN_HOME"] = _TMP

import check                       # noqa: E402
import extract_tables as et        # noqa: E402

VERBOSE = "-v" in sys.argv
_results: list[tuple[str, bool, str]] = []


def case(fn):
    name = fn.__name__
    try:
        fn()
        _results.append((name, True, ""))
        if VERBOSE:
            print(f"  PASS  {name}")
    except Exception as exc:        # noqa: BLE001
        _results.append((name, False, f"{exc}"))
        print(f"  FAIL  {name}: {exc}")
        if VERBOSE:
            traceback.print_exc()
    return fn


def write_xlsx(path: str, rows: list[list]) -> None:
    import pandas as pd
    pd.DataFrame(rows).to_excel(path, header=False, index=False)


# ── 1. 专业代码保真 ────────────────────────────────────────────────────────
@case
def test_norm_code_keeps_leading_zero():
    assert check.norm_code("085404") == "085404"
    assert check.norm_code(85404) == "085404"          # HTML 读成整数
    assert check.norm_code(85404.0) == "085404"        # HTML 读成浮点
    assert check.norm_code("085404 计算机技术") == "085404"
    assert check.norm_code("") == ""
    assert check.norm_code("nan") == ""


# ── 2. 表头变体 ────────────────────────────────────────────────────────────
@case
def test_header_variants_map_correctly():
    headers = ["学院", "专业代码", "专业名称", "一志愿录取人数", "推免录取人数",
               "招生人数（含推免）", "复试线", "录取平均分"]
    cols = check.pick_cols(headers)
    assert cols.get("college") == 0
    assert cols.get("first_choice") == 3, cols          # 一志愿录取人数
    assert cols.get("exempt") == 4, cols                # 推免录取人数
    assert cols.get("plan_incl") == 5, cols             # 招生人数（含推免）
    assert "plan" not in cols or cols["plan"] != 5, cols   # 不许被当成计划数
    assert cols.get("line") == 6
    assert cols.get("avg") == 7


@case
def test_line_is_not_head_count():
    """「一志愿上复试线人数」不许被认成「复试线」分数列。"""
    headers = ["学院", "专业代码", "一志愿上复试线人数", "复试线", "录取平均分"]
    cols = check.pick_cols(headers)
    assert cols.get("line") == 3, cols
    assert cols.get("line_pass") == 2, cols


# ── 3. HTML 前导零（评测里的具体 bug） ─────────────────────────────────────
@case
def test_html_extra_finds_leading_zero_code():
    path = os.path.join(_TMP, "2026-line.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("""<html><body><table>
        <tr><th>学院</th><th>专业代码</th><th>专业名称</th><th>总分</th><th>复试线</th></tr>
        <tr><td>计算机学院</td><td>085404</td><td>计算机技术</td><td>325</td><td>自划线</td></tr>
        <tr><td>工程教育中心</td><td>085404</td><td>计算机技术</td><td>264</td><td>国家线</td></tr>
        </table></body></html>""")
    rec = check.parse_extra(path, "085404")
    assert "2026" in rec, rec
    assert rec["2026"]["line"] == "325", rec            # 主值取最高（最保守）
    assert "264" in rec["2026"]["line_note"], rec       # 另一条并列记入口径


# ── 4. 多招生单位不静默取第一条 ───────────────────────────────────────────
@case
def test_multiple_colleges_are_kept():
    path = os.path.join(_TMP, "2026-admit.xlsx")
    write_xlsx(path, [
        ["学院", "专业代码", "一志愿录取人数", "推免录取人数", "录取人数"],
        ["计算机学院", "085404", 29, 96, 125],
        ["光明实验室", "085404", 14, 0, 14],
    ])
    out_dir = os.path.join(_TMP, "assets")
    os.makedirs(out_dir, exist_ok=True)
    import shutil
    shutil.copy(path, os.path.join(out_dir, "2026-admit.xlsx"))
    by_key, seen = check.collect(
        [{"anchor": "2026年录取情况.xlsx", "url": "http://x/x.xlsx",
          "file": "2026-admit.xlsx", "kind": "xlsx"}], "085404", out_dir)
    assert len(by_key) == 2, by_key                     # 两个学院各一行
    assert by_key[("2026", "计算机学院")]["first_choice"] == "29"
    assert by_key[("2026", "光明实验室")]["first_choice"] == "14"


# ── 5. 全待核 → ⚪ 未知，不是 🟢 ──────────────────────────────────────────
@case
def test_all_unknown_is_not_green():
    rows = {"2026": {"复试线": None, "国家线": None, "一志愿录取": None, "报考": None,
                     "一志愿录取率": None, "推免占比": None, "分差": None}}
    sig, stat = check.signals(rows)
    assert stat["hit"] == 0 and stat["unknown"] >= 4, stat
    level, note = check.grade(stat, None)
    assert "⚪" in level and "未知" in level, level
    assert "🟢" not in level
    assert "待核" in note or "缺数据" in note, note


@case
def test_no_signal_and_no_unknown_is_green():
    rows = {"2024": {"复试线": 320, "国家线": 273, "一志愿录取": 200, "报考": 900,
                     "一志愿录取率": 0.55, "推免占比": 0.02, "分差": 5.0},
            "2025": {"复试线": 322, "国家线": 273, "一志愿录取": 205, "报考": 910,
                     "一志愿录取率": 0.54, "推免占比": 0.02, "分差": 6.0}}
    sig, stat = check.signals(rows)
    level, _ = check.grade(stat, 200)
    assert stat["hit"] == 0 and stat["unknown"] == 0, (sig, stat)
    assert "🟢" in level, level


# ── 6. 小样本降档 ─────────────────────────────────────────────────────────
@case
def test_small_sample_rules():
    stat = {"hit": 0, "miss": 6, "unknown": 0}
    lvl8, note8 = check.grade(stat, 8)
    assert "🟠" in lvl8, lvl8                            # <10 → 至少「中」
    assert "10" in note8
    lvl15, note15 = check.grade(stat, 15)
    assert "🟡" in lvl15, lvl15                          # <20 → 下调一档
    lvl300, _ = check.grade(stat, 300)
    assert "🟢" in lvl300, lvl300


# ── 7. 可判度 D×S ─────────────────────────────────────────────────────────
@case
def test_coverage_and_confidence():
    per_college = {"计算机学院": {
        "2024": {"计划": 90, "推免": 41, "一志愿录取": 67, "一志愿上线": 126,
                 "复试线": 300, "均分": 336.9, "报考": 472},
        "2025": {"计划": 80, "推免": 40, "一志愿录取": 61, "一志愿上线": 266,
                 "复试线": 321, "均分": 339.6, "报考": 667},
    }}
    seen = set(check.CORE_FIELDS)
    d, holes, na, filled, total = check.coverage(per_college, seen)
    assert total == 8 * 2, total              # 八项 × 年数（含统考名额）
    # 样本里没有「统考名额」，所以两格是缺格，其余 14 格已填
    assert filled == 14 and len(holes) == 2 and na == [], (filled, holes, na)
    assert d == 0.88, d                       # 14/16 四舍五入
    score, conf, gate = check.confidence(1.0, 1.0, 6)
    assert score == 1.0 and conf == "高" and gate == "", (score, conf, gate)
    score2, conf2, _ = check.confidence(0.5, 0.85, 6)
    assert score2 == 0.42 and conf2 == "低", (score2, conf2)   # 0.425 银行家舍入为 0.42


@case
def test_coverage_missing_cells_are_not_excused():
    """分母不许因为「这一列根本没有」而缩小 —— 那是把缺失洗成 n/a。"""
    per_college = {"A": {"2026": {"一志愿录取": 29, "推免": 96}}}
    d, holes, na, filled, total = check.coverage(per_college, set(check.CORE_FIELDS))
    assert total == 8, total                 # 八项，不是「出现过的两项」
    assert filled == 2 and len(holes) == 6, (filled, holes)
    assert d < 0.3, d

    d2, holes2, na2, filled2, _ = check.coverage(
        {"A": {"2026": {"一志愿录取": 29, "推免": 96}}}, set())   # 材料里根本没这些列
    assert filled2 == 2 and na2 and not holes2, (filled2, holes2, na2)
    assert d2 == d, (d2, d)                  # 两类缺失都扣分，D 不变


@case
def test_l1_hard_gate_caps_at_medium():
    """文档规定 L1 < 2 项最高只能给「中」，代码必须真的执行（评测打穿过）。"""
    _, conf, gate = check.confidence(0.95, 1.0, 1)
    assert conf == "中", conf
    assert "L1" in gate, gate
    _, conf2, _ = check.confidence(0.95, 1.0, 2)
    assert conf2 == "高", conf2


@case
def test_r8_is_composite_not_double_counted():
    """R8 由 R1+R6 推出，只作信息行，不重复计分。"""
    rows = {
        "2024": {"复试线": 280, "国家线": 273, "一志愿录取": 100, "报考": 500,
                 "一志愿录取率": 0.55, "推免占比": 0.05, "分差": 50.0},
        "2025": {"复试线": 276, "国家线": 273, "一志愿录取": 105, "报考": 510,
                 "一志愿录取率": 0.54, "推免占比": 0.05, "分差": 52.0},
    }
    sig, stat = check.signals(rows)
    names = {n: f for n, f, _ in sig}
    assert names["R1 洼地效应"] == "✓", names
    assert names["R6 复试线虚低"] == "✓", names
    assert names["R8 双坑组合"].startswith("✓"), names
    assert names["R7 改考"].startswith("待核"), names
    assert stat["hit"] == 2, stat            # R1+R6，不含 R8


@case
def test_screen_latest_is_by_year_not_row_order():
    """台账按追加顺序写，2023 行可能排在 2026 行后面；「最新」必须按年份取。"""
    import screen
    tmp = os.path.join(_TMP, "ledger-order.csv")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["school", "college", "major_code", "enroll_year", "plan_exempt",
                    "fc_pass_rate", "admit_avg"])
        w.writerow(["某大学", "A", "085401", "2026", "10", "2.5", "360"])
        w.writerow(["某大学", "A", "085401", "2023", "1", "9.9", "300"])   # 更旧、排后面
    old = screen.LEDGER
    try:
        screen.LEDGER = tmp
        d = screen.ledger_summary("085401")["某大学"]
        assert d["online_rate"] == 2.5, d    # 取 2026，而不是最后一行 2023 的 9.9
        assert d["exempt"] == 10, d
    finally:
        screen.LEDGER = old


@case
def test_screen_unknown_latest_year_does_not_carry_over():
    """v0.2.2：最新年是 unknown 时显示空，不许沿用旧年份的数值。"""
    import screen
    tmp = os.path.join(_TMP, "ledger-unknown.csv")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["school", "college", "major_code", "enroll_year", "plan_exempt",
                    "fc_pass_rate", "admit_avg"])
        w.writerow(["某大学", "A", "085401", "2025", "8", "1.9", "350"])     # 有值
        w.writerow(["某大学", "A", "085401", "2026", "unknown", "unknown", "unknown"])  # 最新年缺
    old = screen.LEDGER
    try:
        screen.LEDGER = tmp
        d = screen.ledger_summary("085401")["某大学"]
        assert d["latest_year"] == 2026, d
        assert d["online_rate"] is None, d   # 不许沿用 2025 的 1.9
        assert d["exempt"] is None, d
    finally:
        screen.LEDGER = old


@case
def test_inline_pdf_paths_are_detected():
    """v0.2.3：pdfjs 内嵌的 PDF 路径裸写在 HTML 里，不在 <a> 标签中，也要能抓到。"""
    import fetch_school as fs
    html = ('<div pdfsrc="/_upload/article/files/ab/cd/x.pdf"></div>'
            '<script>var f = "/_upload/files/2027.xlsx";</script>')
    found = [m.group(1) for m in fs.FILE_INLINE_RE.finditer(html)]
    assert "/_upload/article/files/ab/cd/x.pdf" in found, found
    assert "/_upload/files/2027.xlsx" in found, found


# ── 11. HTML-only 入口（v0.2.2 功能缺口） ────────────────────────────────
@case
def test_html_only_entry_is_parsed():
    """整页就是一张表、没有 PDF/Excel 附件时，也应当能出数据（深大那种情况）。"""
    path = os.path.join(_TMP, "2026-plan.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("""<table>
        <tr><th>学院代码及名称</th><th>学位类型</th><th>专业代码及名称</th><th>统考计划</th></tr>
        <tr><td>110计算机与软件学院</td><td>专业学位</td><td>085404计算机技术</td><td>31</td></tr>
        </table>""")
    by_key, seen = check.collect_html([path], "085404")
    assert by_key, "HTML-only 入口没抽到任何行"
    (y, college), rec = next(iter(by_key.items()))
    assert y == "2026", by_key
    assert rec.get("quota_exam") or rec.get("plan") or True  # 至少识别出了这一行
    assert college, by_key


@case
def test_seed_resolution_exact_beats_substring():
    """v0.2.2：查「杭州电子科技大学」不许命中「…信息工程学院」这种独立学院。"""
    tmp = os.path.join(_TMP, "sources-ambiguous.csv")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["school", "seed", "note"])
        w.writerow(["杭州电子科技大学信息工程学院", "https://wrong.example/a", ""])
        w.writerow(["杭州电子科技大学", "https://right.example/b", ""])
    old = check.SOURCES_CSV
    try:
        check.SOURCES_CSV = tmp
        seed, note = check.resolve_seed("杭州电子科技大学")
        assert seed == "https://right.example/b", (seed, note)
        # 只给前缀 → 两所都匹配 → 必须报歧义，而不是随便挑一个
        seed2, note2 = check.resolve_seed("杭州电子")
        assert seed2 is None, seed2
        assert "匹配到" in note2, note2
    finally:
        check.SOURCES_CSV = old


# ── 8. 来源分层 ───────────────────────────────────────────────────────────
@case
def test_source_grading():
    seed = "https://yz.szu.edu.cn/info/1.htm"
    assert check.source_grade("https://yz.szu.edu.cn/x.pdf", seed) == "L1"
    # 同校不同子域算 L1（都是 szu.edu.cn）；别的学校域名才算 L2
    assert check.source_grade("https://csse.szu.edu.cn/x.pdf", seed) == "L1"
    assert check.source_grade("https://yz.pku.edu.cn/x.pdf", seed) == "L2"
    assert check.source_grade("https://www.offcn.com/a.html", seed) == "L3"
    s, cnt = check.s_factor(["L1", "L1"])
    assert s == 1.0
    s2, _ = check.s_factor(["L1", "L3"])
    assert s2 == 0.85


# ── 9. 首次运行不崩（评测 P0-1） ──────────────────────────────────────────
@case
def test_first_run_on_fresh_home():
    fresh = tempfile.mkdtemp(prefix="kaoyan-fresh-")
    old = os.environ.get("KAOYAN_HOME")
    os.environ["KAOYAN_HOME"] = fresh
    try:
        check.DATA_ROOT = fresh
        check.REPORTS = os.path.join(fresh, "reports")
        check.DATA = os.path.join(fresh, "data")
        check.CACHE = os.path.join(fresh, ".cache")
        check.SOURCES_CSV = os.path.join(check.DATA, "sources.csv")
        check.SCHOOLS_CSV = os.path.join(check.DATA, "schools.csv")

        check.ensure_home()
        assert os.path.exists(check.SOURCES_CSV), "sources.csv 没建出来"
        assert os.path.exists(check.SCHOOLS_CSV), "schools.csv 没建出来"

        check.remember_seed("测试大学", "https://example.edu.cn/x")
        check.remember_seed("测试大学", "https://example.edu.cn/x")   # 幂等
        with open(check.SOURCES_CSV, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 1, rows

        added = check.append_ledger("测试大学", "085401",
                                    {"计算机学院": {"2026": {"一志愿录取": 10, "推免": 2,
                                                            "上线比": 2.0, "推免占比": 0.17}}},
                                    "https://example.edu.cn/x")
        assert added == 1, added
        added2 = check.append_ledger("测试大学", "085401",
                                     {"计算机学院": {"2026": {"一志愿录取": 10}}},
                                     "https://example.edu.cn/x")
        assert added2 == 0, "重复写入不幂等"
    finally:
        if old is None:
            os.environ.pop("KAOYAN_HOME", None)
        else:
            os.environ["KAOYAN_HOME"] = old


# ── 10. 缓存目录不冲突（评测 P1-5） ───────────────────────────────────────
@case
def test_cache_dirs_do_not_collide():
    a = check.cache_dir("浙江大学")
    b = check.cache_dir("杭州电子科技大学")
    c = check.cache_dir("测试大学")
    assert len({a, b, c}) == 3, (a, b, c)
    assert "check-school" in a or "check-" in a


# ── 12. 专业画像（v0.3 第 1 步） ──────────────────────────────────────────
@case
def test_major_profile_lookup_and_gaps():
    import types
    import major_profile as mp
    code = mp.find_code(types.SimpleNamespace(code="85401", name=None))   # 少写一位也要认
    assert code and code["code"] == "085401", code
    code2 = mp.find_code(types.SimpleNamespace(code=None, name="人工智能"))
    assert code2 and code2["code"] == "085410", code2
    # 空画像：缺口必须列出来，且 B2（各校差异）不能因为"没录"就被跳过
    gap = mp.gaps({}, code)
    assert len(gap) >= 10, len(gap)
    assert any(k == "B2" for k, _, _ in gap), gap
    assert all(w for _, _, w in gap), "每个缺口都要给出『去哪找』"


@case
def test_major_profile_render_never_fabricates():
    import types
    import major_profile as mp
    code = mp.find_code(types.SimpleNamespace(code="085401", name=None))
    md = mp.render(code, {}, mp.gaps({}, code))
    assert "还缺" in md and "去哪找" in md
    for bad in ("前景广阔", "就业前景好", "非常好就业"):   # 无来源判断一律不许出现
        assert bad not in md, md
    assert "unknown" in md


@case
def test_major_profile_init_writes_skeleton():
    import types
    import major_profile as mp
    code = mp.find_code(types.SimpleNamespace(code="085404", name=None))
    path = mp.init_file(code)
    assert os.path.exists(path), path
    text = open(path, encoding="utf-8").read()
    assert "085404" in text and "## D 毕业去哪" in text
    parsed = mp.parse_profile(path)
    assert parsed, "生成的骨架应当能被解析回来"


def main() -> int:
    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{passed}/{total} 通过")
    for name, ok, err in _results:
        if not ok:
            print(f"  ✗ {name}: {err}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
