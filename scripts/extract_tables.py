#!/usr/bin/env python3
"""普适解析器：把附件拆成表格，再把表头映射到统一字段。

同样不认识具体学校。策略分两层：
1. **拆表**：PDF 用 pdfplumber（按页逐表提取，失败退回按行切文本）；Excel 走 pandas 全部 sheet；CSV 直读
2. **认列**：列名用模糊词表匹配到标准字段（专业代码/名称、计划、推免、统考、复试线、最高/最低/平均分、国家线）

认不出列的表照样输出，只是标 unmapped —— 让下一层（人或 LLM）接手，而不是丢数据。

用法：
  python extract_tables.py --file path/to/目录.pdf --out out/tables
  python extract_tables.py --file 目录.pdf --out out/tables --major 085404
"""
from __future__ import annotations

import argparse
import json
import io
import os
import re
import sys

import pandas as pd
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def read_html_tables(src: str) -> list[pd.DataFrame]:
    """v0.2 修复两件事：

    1. `pd.read_html` 默认按 latin-1 解码，中文列名会整列乱码 → 一个别名都匹配不上
       （外部评测「HTML 复试线页抽不到 085404」的根因之一）。
    2. `pd.read_html(str)` 会把字符串当**文件名/URL**，必须用 StringIO 包一层。

    URL：requests 取回后按响应推断编码解码；本地文件：依次试 utf-8 / gb18030 / gbk。
    """
    if src.startswith(("http://", "https://")):
        r = requests.get(src, timeout=30, headers={"User-Agent": UA})
        r.encoding = r.apparent_encoding or "utf-8"
        return pd.read_html(io.StringIO(r.text))
    with open(src, "rb") as fh:
        raw = fh.read()
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return pd.read_html(io.StringIO(raw.decode(enc)))
        except UnicodeDecodeError as exc:
            last_err = exc
            continue
    raise RuntimeError(f"HTML 编码无法识别：{last_err}")

# 标准字段 → 列名里可能出现的词（越靠前越优先）
FIELD_RULES = {
    "major_field": ["学科/专业领域", "学科专业领域", "专业领域", "学科专业", "学科、专业", "专业学位类别"],
    "major_code": ["专业代码", "学科代码", "代码"],
    "major_name": ["专业名称", "学科专业", "专业", "研究方向"],
    "college": ["招生学院", "学院", "院系", "招生单位", "培养单位"],
    "plan_total": ["拟招生人数", "简章计划", "拟招生", "招生计划", "计划数", "计划", "招生人数"],
    "plan_exempt": ["推免", "免试", "保研"],
    "quota_exam": ["统考", "剩余", "可用于统考", "考试招生"],
    "line": ["复试线", "复试分数", "分数线", "院线", "校线"],
    "admit_max": ["最高分"],
    "admit_min": ["最低分"],
    "admit_avg": ["平均分", "均分"],
    "national_line": ["国家线"],
    "applicants": ["网报", "报考人数", "报名人数"],
    "retest_count": ["复试人数", "实际复试"],
}
MUST_HAVE = ("major_code", "major_name")
# 抢词排除：同一张表里常有「学院代码」和「专业代码」并列，
# 只按子串匹配会把 major_code 抢到学院代码上。给易混字段加否决词。
EXCLUDE = {
    "major_code": ("学院", "院系", "单位", "考试"),
    "major_name": ("学院", "研究方向方向"),
    "college": ("代码",),
    "plan_total": ("已接收", "推免"),
    # 「招生人数（含推免）」是计划数，绝不能被当成推免人数
    "plan_exempt": ("含推免", "招生人数", "计划"),
}
CODE_RE = re.compile(r"^\s*(\d{6})\s*[、,，.．]?\s*(.*)$", re.S)
FFILL_FIELDS = ("college", "major_field", "major_code", "major_name")


def promote_header(df: pd.DataFrame) -> pd.DataFrame:
    """表头可能藏在数据行里（网页表格没写 <thead>、Excel 上方有标题行）。

    判据：列名全是 0/1/2… 这种序号，就往下找第一行「像表头」的行——
    该行至少命中 2 个字段关键词。找不到就原样返回，不猜。
    """
    cols = [str(c) for c in df.columns]
    if sum(1 for c in cols if re.fullmatch(r"\d+", c)) < max(1, len(cols) / 2):
        return df
    for i in range(min(8, len(df))):
        row = [str(x) for x in df.iloc[i].tolist()]
        hits = sum(1 for cell in row
                   if any(w in cell for words in FIELD_RULES.values() for w in words))
        if hits >= 2:
            out = df.iloc[i + 1:].copy()
            out.columns = [c if c and c != "nan" else f"c{j}" for j, c in enumerate(row)]
            return out.reset_index(drop=True)
    return df


def normalize(df: pd.DataFrame, mapping: dict[str, int]) -> pd.DataFrame:
    """把各校五花八门的排版归一：去换行、填合并单元格、从合并格拆出专业代码。

    这三件事跟学校无关，任何一份招生目录 PDF 都会遇到。
    注意：表头列名可能重复（每页都印一遍），所以一律按**位置**操作，不按名字。
    """
    df = df.copy()
    df.columns = [f"c{i}" if str(c) in ("", "None") else str(c) for i, c in enumerate(df.columns)]
    for idx in range(df.shape[1]):
        col = df.iloc[:, idx].astype(str)
        col = col.str.replace(r"\s*\n\s*", "", regex=True).str.strip()
        df.iloc[:, idx] = col.replace({"None": "", "nan": ""})

    # 合并单元格：同一学院/专业只在首行出现，下面留空 → 向下填充
    for field in FFILL_FIELDS:
        if field in mapping:
            idx = mapping[field]
            df.iloc[:, idx] = df.iloc[:, idx].replace("", pd.NA).ffill().fillna("")

    # 「081000 信息与通信工程」这种代码名称同格的，拆成两列
    src = None
    for field in ("major_field", "major_name"):
        if field in mapping:
            src = mapping[field]
            break
    if src is not None:
        codes, names = [], []
        for cell in df.iloc[:, src].tolist():
            m = CODE_RE.match(str(cell))
            codes.append(m.group(1) if m else "")
            names.append(m.group(2) if m else "")
        if any(codes) and "major_code" not in mapping:
            df["专业代码"] = codes
            mapping["major_code"] = df.shape[1] - 1
        if any(n.strip() for n in names) and "major_name" not in mapping:
            df["专业名称"] = names
            mapping["major_name"] = df.shape[1] - 1
    return df


def map_headers(headers: list[str]) -> dict[str, int]:
    """列名 → 字段，返回 {字段: 列序号}。一个字段只认第一个命中的列。"""
    out: dict[str, int] = {}
    for idx, raw in enumerate(headers):
        name = re.sub(r"\s+|[()（）◎\*]", "", str(raw))
        if not name:
            continue
        for field, words in FIELD_RULES.items():
            if field in out:
                continue
            if any(x in name for x in EXCLUDE.get(field, ())):
                continue
            if any(w in name for w in words):
                out[field] = idx
                break
    return out


def tables_from_pdf(path: str) -> list[tuple[str, pd.DataFrame]]:
    import pdfplumber
    out: list[tuple[str, pd.DataFrame]] = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, 1):
            try:
                for tno, table in enumerate(page.extract_tables() or [], 1):
                    if not table or len(table) < 2:
                        continue
                    df = pd.DataFrame(table)
                    df.columns = [str(c).replace("\n", "") for c in df.iloc[0]]
                    df = df.iloc[1:].reset_index(drop=True)
                    out.append((f"p{pno}-t{tno}", df))
            except Exception as exc:                    # noqa: BLE001
                print(f"  ! 第 {pno} 页表格提取失败：{exc}", file=sys.stderr)
    if not out:                                        # 没有表格结构 → 退回纯文本
        text = []
        with pdfplumber.open(path) as pdf:
            for pno, page in enumerate(pdf.pages, 1):
                text.append((pno, page.extract_text() or ""))
        if any(t.strip() for _, t in text):
            body = "\n".join(t for _, t in text)
            out.append(("text-only", pd.DataFrame({"line": body.splitlines()})))
    return out


def tables_from_excel(path: str) -> list[tuple[str, pd.DataFrame]]:
    book = pd.read_excel(path, sheet_name=None, header=None, dtype=str)
    out = []
    for sheet, raw in book.items():
        if raw.empty:
            continue
        # 找第一行"像表头"的行：含「专业」或「代码」
        for idx in range(min(12, len(raw))):
            row = [str(c) for c in raw.iloc[idx].tolist()]
            if any(k in "".join(row) for k in ("专业", "代码", "学院", "计划")):
                df = raw.iloc[idx + 1:].copy()
                df.columns = row
                out.append((f"{sheet}", df.reset_index(drop=True)))
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="pdf / xls / xlsx / csv / html 文件")
    ap.add_argument("--url", help="直接给网页 URL，抽页面里的 <table>")
    ap.add_argument("--out", required=True)
    ap.add_argument("--major", default=None, help="只打印含该专业代码的行")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    if args.url:
        tables = [(f"html-t{i}", df.astype(str))
                  for i, df in enumerate(read_html_tables(args.url), 1)]
        src_name = args.url
    else:
        if not args.file:
            ap.error("需要 --file 或 --url")
        src_name = args.file
        ext = os.path.splitext(args.file)[1].lower()
        if ext == ".pdf":
            tables = tables_from_pdf(args.file)
        elif ext in (".xls", ".xlsx"):
            tables = tables_from_excel(args.file)
        elif ext in (".csv", ".html", ".htm"):
            if ext == ".csv":
                tables = [("csv", pd.read_csv(args.file, dtype=str))]
            else:
                tables = [(f"html-t{i}", df.astype(str))
                          for i, df in enumerate(read_html_tables(args.file), 1)]
        else:
            print(f"不支持的类型：{ext}", file=sys.stderr)
            return 2

    index, hits = [], []
    for i, df in enumerate(tables, 1):
        name, df = df
        if df.empty:
            continue
        df = promote_header(df)
        mapping = map_headers(list(df.columns))
        df = normalize(df, mapping)
        stem = re.sub(r"[^0-9A-Za-z_-]", "_", name)
        df.to_csv(os.path.join(args.out, f"{stem}.csv"), index=False, encoding="utf-8-sig")
        index.append({"table": name, "rows": len(df), "cols": len(df.columns),
                      "mapped": list(mapping), "unmapped_ok": all(f in mapping for f in MUST_HAVE)})
        if args.major and "major_code" in mapping:
            col = df.columns[mapping["major_code"]]
            # v0.2 修复：HTML/XLSX 会把 085404 读成整数 85404，比较前统一补零到 6 位
            target = re.sub(r"\D", "", str(args.major)).zfill(6)
            codes = df[col].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6)
            sub = df[codes == target]
            for _, row in sub.head(20).iterrows():
                hits.append({"table": name, **{k: row[df.columns[i]] for k, i in mapping.items()}})

    with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({"source": src_name, "tables": index, "hits": hits},
                  fh, ensure_ascii=False, indent=2)

    print(f"{os.path.basename(src_name)} → {len(index)} 张表")
    for it in index:
        flag = "认列OK" if it["unmapped_ok"] else "缺关键列"
        print(f"  {it['table']:>12}  {it['rows']:>4} 行 × {it['cols']:>2} 列  [{flag}] 映射：{','.join(it['mapped'][:8])}")
    if args.major:
        print(f"\n=== 命中 {args.major}：{len(hits)} 行 ===")
        for h in hits[:12]:
            print("  " + " | ".join(f"{k}={v}" for k, v in h.items() if str(v).strip() and str(v) != "nan"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
