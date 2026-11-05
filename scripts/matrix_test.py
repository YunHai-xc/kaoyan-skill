"""矩阵跑批：4 个专业 × 5 所院校 = 20 次真实跑，输出 CSV 与汇总报告。

用途：定期确认「现在还跑得动哪些学校」。学校改版、加验证码、目录换系统，
结果都会变；跑一次就知道边界挪到哪了。

数据写到同级 `kaoyan-matrix/`（独立于 skill 本体），不污染分发包。
  python scripts/matrix_test.py
改学校/专业：直接编辑下面的 SCHOOLS 与 MAJORS。
"""
import contextlib
import csv
import io
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

DATA = os.path.join(os.path.dirname(ROOT), "kaoyan-matrix")   # 独立数据目录，不污染 skill
os.environ["KAOYAN_HOME"] = os.path.abspath(DATA)
import check  # noqa: E402

SCHOOLS = [
    ("杭州电子科技大学", "https://grs.hdu.edu.cn/2026/0929/c13800a301001/page.psp",
     "http://grs.hdu.edu.cn/2026/0324/c13270a290580/page.htm"),
    ("深圳大学", "https://yz.szu.edu.cn/info/1008/15416.htm", ""),
    ("西安电子科技大学", "https://gr.xidian.edu.cn/info/1074/20804.htm", ""),
    ("山东大学", "https://yz.sdu.edu.cn/info/1004/1487.htm", ""),
    ("南京邮电大学", "https://yzb.njupt.edu.cn/2026/0930/c7801a309247/page.htm", ""),
]
MAJORS = [
    ("085404", "计算机技术"),
    ("085401", "新一代电子信息技术"),
    ("085410", "人工智能"),
    ("085402", "通信工程"),
]

rows = []
for major, mname in MAJORS:
    for school, seed, extra in SCHOOLS:
        argv = ["check.py", "--school", school, "--major", major, "--seed", seed]
        if extra:
            argv += ["--extra", extra]
        sys.argv = argv
        buf = io.StringIO()
        t0 = time.time()
        code, err = None, ""
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = check.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception:                       # noqa: BLE001
            code = 99
            err = traceback.format_exc(limit=1).strip().splitlines()[-1][:120]
        out = buf.getvalue()
        m = re.search(r"可判度 ([\d.]+)×([\d.]+)=([\d.]+)（(\S+?)）｜(.*?)｜台账新增 (\d+)", out)
        d, s, score, conf, level, ledger = (m.groups() if m else ("", "", "", "", "", ""))
        rows.append({
            "major": major, "major_name": mname, "school": school,
            "exit": code, "D": d, "S": s, "score": score, "conf": conf,
            "level": level.replace("｜", "").strip(), "ledger": ledger,
            "seconds": f"{time.time() - t0:.0f}", "note": err,
        })
        print(f"[{len(rows):2}/20] {major} {school:<10} exit={code} D={d or '-'} {level or ''} {rows[-1]['seconds']}s",
              flush=True)

# 结果表
os.makedirs(check.DATA, exist_ok=True)
csv_path = os.path.join(check.DATA, "matrix-test.csv")
with open(csv_path, "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

ok = [r for r in rows if r["exit"] == 0]

# 汇总 Markdown
L = [f"> 跑批日期：{time.strftime('%Y-%m-%d')}｜4 专业 × 5 校 = {len(rows)} 次",
     "> 数据目录：独立于 skill（本文件不写入 skill 目录）", "",
     "# 4×5 矩阵测试", "",
     f"**成功 {len(ok)}/{len(rows)}**（exit=0，即产出了报告与台账行）", "",
     "| 专业 | 学校 | 结果 | D | 置信度 | 风险等级 | 耗时 |",
     "|---|---|---|---:|---|---|---:|"]
for r in rows:
    res = "✅" if r["exit"] == 0 else f"❌ exit={r['exit']}"
    L.append(f"| {r['major']} {r['major_name']} | {r['school']} | {res} | {r['D'] or '—'} "
             f"| {r['conf'] or '—'} | {r['level'] or '—'} | {r['seconds']}s |")
L += ["", "## 按专业看", "", "| 专业 | 成功 | D 范围 |", "|---|---:|---|"]
for major, mname in MAJORS:
    sub = [r for r in rows if r["major"] == major]
    good = [r for r in sub if r["exit"] == 0]
    ds = [r["D"] for r in good if r["D"]]
    rng = f"{min(ds)}–{max(ds)}" if ds else "—"
    L.append(f"| {major} {mname} | {len(good)}/{len(sub)} | {rng} |")
L += ["", "## 按学校看", "", "| 学校 | 成功 | 说明 |", "|---|---:|---|"]
notes = {
    "杭州电子科技大学": "PDF 附件（2024-2026 报考上线录取情况）+ HTML 复试线补充页",
    "深圳大学": "HTML-only 专页（只有统考计划，无分数线/录取分 → D 天然很低）",
    "西安电子科技大学": "PDF 招生目录（有计划数、无录取分布）",
    "山东大学": "简章页附件（目录锁 CAS，能拿到的是页内 PDF）",
    "南京邮电大学": "2027 招生目录页（新站点，首次测试）",
}
for school, _, _ in SCHOOLS:
    sub = [r for r in rows if r["school"] == school]
    good = [r for r in sub if r["exit"] == 0]
    L.append(f"| {school} | {len(good)}/{len(sub)} | {notes.get(school, '')} |")
failed = [r for r in rows if r["exit"] != 0]
if failed:
    L += ["", "## 失败明细", "", "| 专业 | 学校 | exit | 原因 |", "|---|---|---:|---|"]
    for r in failed:
        L.append(f"| {r['major']} | {r['school']} | {r['exit']} | {r['note'] or '脚本在抓取/解析阶段提前退出'} |")
L += ["", "> 只证明「能跑通并出报告」；结论是否正确需人工回原文核对（见每份报告「来源清单」）。"]

md = "\n".join(L)
os.makedirs(check.REPORTS, exist_ok=True)
out_md = os.path.join(check.REPORTS, f"{time.strftime('%Y-%m-%d')}-matrix-4x5.md")
with open(out_md, "w", encoding="utf-8") as fh:
    fh.write(md)
print(f"\n成功 {len(ok)}/{len(rows)}")
print(f"→ {csv_path}")
print(f"→ {out_md}")
