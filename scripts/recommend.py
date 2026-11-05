#!/usr/bin/env python3
"""推荐入口：从「你已有的东西」出发收敛，而不是从 936 所里大海捞针。

三种学生状态，三种入口：
  A. 我已经有几所目标校      → --schools 杭电 南邮 上大      （直接对比，不再筛）
  B. 我只知道想去哪           → --city 杭州 上海 或 --province 浙江
  C. 我只知道自己的本科专业    → --undergrad-major 电子信息工程（先映射到专业代码）

**它不做排名。** 没有全国统一的院校质量数据，任何名次都是编的。
它做的是三层过滤，每层都要回答「为什么进、为什么出」：

  ① 可得性：已建档（有数据） / 可点火（有入口页） / 待点火 / 已知查不到（附原因）
  ② 风险信号：只有已建档的能给，D×S + R1~R8，缺什么直说
  ③ 你的约束：城市 / 层次 / 学费与宿舍（后者需逐校查 life.md，本脚本只列待查）

用法：
  python recommend.py --schools 杭州电子科技大学 南京邮电大学 --major 085401
  python recommend.py --city 杭州 南京 上海 --major 085401
  python recommend.py --undergrad-major 电子信息工程 --province 浙江 江苏
  python recommend.py --major 085401 --province 浙江 江苏 上海 北京 天津   # 读 filters.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# v0.4.0：默认数据目录放到技能**外面**（同级 kaoyan-data/），技能包保持只读干净。
# 设了 KAOYAN_HOME 就听它的。
DATA_ROOT = os.path.abspath(os.environ.get("KAOYAN_HOME")
                            or os.path.join(os.path.dirname(ROOT), "kaoyan-data"))
DATA = os.path.join(DATA_ROOT, "data")
REPORTS = os.path.join(DATA_ROOT, "reports")
FILTERS = os.path.join(DATA_ROOT, "assets", "filters.json")

# 城市 → 省份（研招网池子按省聚合；直辖市同名）
CITY_PROVINCE = {
    "北京": "北京", "上海": "上海", "天津": "天津", "重庆": "重庆",
    "广州": "广东", "深圳": "广东", "珠海": "广东", "东莞": "广东", "佛山": "广东",
    "杭州": "浙江", "宁波": "浙江", "温州": "浙江", "嘉兴": "浙江", "绍兴": "浙江",
    "南京": "江苏", "苏州": "江苏", "无锡": "江苏", "常州": "江苏", "南通": "江苏", "徐州": "江苏",
    "合肥": "安徽", "福州": "福建", "厦门": "福建", "济南": "山东", "青岛": "山东",
    "武汉": "湖北", "长沙": "湖南", "成都": "四川", "西安": "陕西", "郑州": "河南",
    "沈阳": "辽宁", "大连": "辽宁", "长春": "吉林", "哈尔滨": "黑龙江",
    "石家庄": "河北", "太原": "山西", "南昌": "江西", "南宁": "广西",
    "昆明": "云南", "贵阳": "贵州", "兰州": "甘肃", "乌鲁木齐": "新疆",
}

# 本科专业 → 可能的报考专业代码（只做「起手式」，最终要用户确认）
# 本科专业 → 对口方向（不跨考时的候选集合，v0.3.2 大幅扩充）
# 顺序有意义：第一个是最对口的。跨考时才需要放宽到更远的门类。
UNDERGRAD_MAP = {
    # ── 电子/通信/计算机 ──
    "电子信息工程": [("085401", "新一代电子信息技术"), ("085402", "通信工程"),
                     ("085403", "集成电路工程"), ("080900", "电子科学与技术（学硕）")],
    "电子科学与技术": [("085403", "集成电路工程"), ("085401", "新一代电子信息技术"),
                       ("080900", "电子科学与技术（学硕）")],
    "电子信息科学与技术": [("085401", "新一代电子信息技术"), ("085403", "集成电路工程")],
    "通信工程": [("085402", "通信工程"), ("085401", "新一代电子信息技术"),
                 ("081000", "信息与通信工程（学硕）")],
    "计算机科学与技术": [("085404", "计算机技术"), ("081200", "计算机科学与技术（学硕）")],
    "软件工程": [("085405", "软件工程"), ("085404", "计算机技术")],
    "网络工程": [("085412", "网络与信息安全"), ("085404", "计算机技术")],
    "信息安全": [("085412", "网络与信息安全"), ("085404", "计算机技术")],
    "人工智能": [("085410", "人工智能"), ("085404", "计算机技术")],
    "物联网工程": [("085404", "计算机技术"), ("085402", "通信工程")],
    "数据科学与大数据": [("085411", "大数据技术与工程"), ("085404", "计算机技术")],
    # ── 自动化 / 电气 / 机械 / 材料 / 土木 / 化工 / 环境 / 生物 ──
    "自动化": [("085406", "控制工程"), ("085410", "人工智能")],
    "电气工程": [("085801", "电气工程"), ("085406", "控制工程")],
    "机械": [("085501", "机械工程")],
    "材料": [("085601", "材料工程")],
    "土木": [("085901", "土木水利")],
    "化学": [("070300", "化学（学硕）"), ("085602", "化学工程")],
    "化学工程": [("085602", "化学工程"), ("070300", "化学（学硕）")],
    "环境": [("085701", "环境工程")],
    "生物": [("086000", "生物与医药"), ("085409", "生物医学工程")],
    "光电信息": [("085408", "光电信息工程")],
    "微电子": [("085403", "集成电路工程")],
    "仪器": [("085407", "仪器仪表工程")],
    # ── 理学 ──
    "数学": [("070100", "数学（学硕）"), ("025200", "应用统计")],
    "统计": [("025200", "应用统计"), ("071400", "统计学（学硕）")],
    "物理": [("070200", "物理学（学硕）")],
    # ── 法学 ──
    "法学": [("030100", "法学（学硕）"), ("035102", "法律（法学）")],
    "知识产权": [("030100", "法学（学硕）"), ("035102", "法律（法学）")],
    "政治学": [("030200", "政治学（学硕）")],
    "社会学": [("030300", "社会学（学硕）")],
    "马克思主义": [("030500", "马克思主义理论（学硕）")],
    "思想政治教育": [("030500", "马克思主义理论（学硕）")],
    # ── 文学 / 历史 / 哲学 / 教育 / 心理 ──
    "汉语言文学": [("050100", "中国语言文学（学硕）"), ("045300", "国际中文教育")],
    "汉语国际教育": [("045300", "国际中文教育"), ("050100", "中国语言文学（学硕）")],
    "新闻": [("055200", "新闻与传播"), ("050300", "新闻传播学（学硕）")],
    "传播": [("055200", "新闻与传播"), ("050300", "新闻传播学（学硕）")],
    "英语": [("055100", "翻译"), ("050200", "外国语言文学（学硕）")],
    "日语": [("055100", "翻译"), ("050200", "外国语言文学（学硕）")],
    "历史": [("060200", "中国史（学硕）"), ("060300", "世界史（学硕）")],
    "哲学": [("010100", "哲学（学硕）")],
    "教育学": [("040100", "教育学（学硕）"), ("045100", "教育")],
    "教育技术": [("045100", "教育"), ("040100", "教育学（学硕）")],
    "心理": [("040200", "心理学（学硕）"), ("045400", "应用心理")],
    # ── 经管 ──
    "会计": [("125300", "会计"), ("120201", "会计学（学硕）")],
    "金融": [("025100", "金融")],
    "经济": [("020100", "理论经济学（学硕）"), ("025100", "金融")],
    "工商管理": [("125100", "工商管理"), ("120200", "工商管理学（学硕）")],
    "公共管理": [("125200", "公共管理"), ("120400", "公共管理学（学硕）")],
    "市场营销": [("120200", "工商管理学（学硕）"), ("125100", "工商管理")],
    # ── 医学 ──
    "临床医学": [("105100", "临床医学")],
    "护理": [("105400", "护理")],
    "药学": [("105500", "药学"), ("100700", "药学（学硕）")],
    "中医": [("105700", "中医")],
}


def read_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def provinces_of(args, filters: dict) -> list[str]:
    out = list(args.province or [])
    for city in (args.city or []):
        prov = CITY_PROVINCE.get(city)
        if prov and prov not in out:
            out.append(prov)
    if not out:
        out = list(filters.get("provinces") or [])
    return out


def ledger_index() -> dict[str, dict]:
    """台账里每所学校的已有数据（按专业代码隔离）。"""
    out: dict[str, dict] = {}
    for r in read_csv(os.path.join(DATA, "schools.csv")):
        name = (r.get("school") or "").strip()
        if not name or name == "unknown":
            continue
        d = out.setdefault(name, {"majors": set(), "years": {}, "latest_year": 0})
        code = (r.get("major_code") or "").strip()
        if code and code != "unknown":
            d["majors"].add(code)
        try:
            y = int(str(r.get("enroll_year") or 0))
        except ValueError:
            y = 0
        d["years"][y] = {
            "line": r.get("line_major"), "avg": r.get("admit_avg"),
            "exempt": r.get("plan_exempt"), "online": r.get("fc_pass_rate"),
        }
        d["latest_year"] = max(d["latest_year"], y)
    return out


def seed_index() -> dict[str, tuple[str, str]]:
    """学校 → (入口页, 来源)。用户注册表优先。"""
    out: dict[str, tuple[str, str]] = {}
    for path, tag in ((os.path.join(DATA, "sources.csv"), "已注册"),
                      (os.path.join(ROOT, "data", "seeds.builtin.csv"), "内置")):
        for r in read_csv(path):
            name = (r.get("school") or "").strip()
            seed = r.get("seed")
            if name and seed and name not in out:
                out[name] = (seed, tag)
    return out


def known_blocked() -> dict[str, str]:
    """已知拿不到的学校 → 原因（写死在 reason 里，便于复跑时对照）。"""
    out = {}
    for r in read_csv(os.path.join(ROOT, "data", "seeds.builtin.csv")):
        name = (r.get("school") or "").strip()
        status = (r.get("last_status") or "").strip()
        if name and status in ("failed",):
            out[name] = (r.get("type") or "") + "：" + (r.get("note") or "")
    return out


def load_major_codes(path: str) -> dict[str, dict]:
    out = {}
    for r in read_csv(path):
        code = (r.get("code") or "").strip()
        if code:
            out[code] = r
    return out


def print_directions(undergrad: str, cross: bool) -> int:
    """本科专业 → 对口方向清单。不跨考时，范围在这里就定死了，不用再问"想考什么"。"""
    key = (undergrad or "").strip()
    # 先精确匹配，再「本科专业包含映射键」，最后「映射键包含本科专业」
    # （不这样分优先级，"化学" 会被 "化学工程" 抢走）
    hit = key if key in UNDERGRAD_MAP else None
    if not hit:
        cands = [k for k in UNDERGRAD_MAP if k in key]
        if not cands:
            cands = [k for k in UNDERGRAD_MAP if key in k]
        hit = max(cands, key=len) if cands else None
    if not hit:
        print(f"「{key}」不在内置对口表里。已收录的本科专业：", file=sys.stderr)
        print("  " + "、".join(UNDERGRAD_MAP), file=sys.stderr)
        return 2
    codes = load_major_codes(os.path.join(ROOT, "data", "major_codes.csv"))
    rows = UNDERGRAD_MAP[hit]
    print(f"# 本科「{hit}」→ 对口方向（{'跨考放宽' if cross else '不跨考'}）\n")
    print("| # | 代码 | 方向 | 学位 | 报考限制 | 备注 |")
    print("|---:|---|---|---|---|---|")
    for i, (code, name) in enumerate(rows, 1):
        meta = codes.get(code, {})
        print(f"| {i} | {code} | {name} | {meta.get('degree', '—')} "
              f"| {meta.get('restriction') or '—'} | {meta.get('note', '')[:28] or '—'} |")
    print()
    print("**顺序有意义**：第一行是最对口的。不跨考时，你只需要在这几行里挑一个，")
    print("不用再回答「想考什么专业」——它已经由你的本科专业决定了。")
    if rows and codes.get(rows[0][0], {}).get("restriction"):
        print(f"\n> ⚠️ 注意 {rows[0][0]} 的报考限制：{codes[rows[0][0]]['restriction']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="推荐：从已有条件收敛，不做排名")
    ap.add_argument("--schools", nargs="*", default=[], help="已有目标校")
    ap.add_argument("--city", nargs="*", default=[], help="想去的城市")
    ap.add_argument("--province", nargs="*", default=[], help="想去的省份")
    ap.add_argument("--major", default="", help="专业代码")
    ap.add_argument("--undergrad-major", default="", help="本科专业，用于映射专业代码")
    ap.add_argument("--directions", action="store_true",
                    help="只输出「本科专业 → 对口方向清单」，不查学校（不跨考时先做这个）")
    ap.add_argument("--cross", action="store_true", help="跨考：放宽到相邻门类（输出更大范围）")
    ap.add_argument("--top", type=int, default=15, help="清单最多列几所")
    ap.add_argument("--level", default="", help="层次要求：双一流 / 不限")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    filters: dict = {}
    if os.path.exists(FILTERS):
        with open(FILTERS, encoding="utf-8") as fh:
            filters = json.load(fh)

    if args.directions:
        if not args.undergrad_major:
            print("--directions 需要配 --undergrad-major（你的本科专业）", file=sys.stderr)
            return 2
        return print_directions(args.undergrad_major, args.cross)

    # —— 专业：代码 > 本科专业映射 > filters ——
    majors: list[tuple[str, str]] = []
    if args.major:
        majors = [(args.major, "")]
    elif args.undergrad_major:
        for key, codes in UNDERGRAD_MAP.items():
            if key in args.undergrad_major or args.undergrad_major in key:
                majors = codes
                break
        if not majors:
            print(f"「{args.undergrad_major}」不在内置映射里，请直接用 --major 指定六位专业代码。",
                  file=sys.stderr)
            return 2
    elif filters.get("target", {}).get("major_code"):
        majors = [(filters["target"]["major_code"], filters["target"].get("major_name", ""))]
    if not majors:
        print("请至少给一个专业：--major 085401 或 --undergrad-major 电子信息工程", file=sys.stderr)
        return 2

    provinces = provinces_of(args, filters)
    pool = read_csv(os.path.join(DATA, "pool.csv"))
    ledger = ledger_index()
    seeds = seed_index()
    blocked = known_blocked()

    lines: list[str] = []
    lines.append(f"> 生成：{dt.date.today()}｜专业：{'、'.join(f'{c}{n}' for c, n in majors)}"
                 f"｜范围：{'、'.join(provinces) or '不限'}")
    lines.append("")
    lines.append("# 推荐单")
    lines.append("")
    lines.append("**这不是排名。** 名单按「能不能拿到数据」分层，不是按学校好坏排序——"
                 "没有全国统一的质量数据，任何名次都是编的。")
    lines.append("")

    # 0. 回显输入
    lines.append("## 0. 我理解的条件")
    lines.append("")
    lines.append("| 你给的 | 我理解成 | 说明 |")
    lines.append("|---|---|---|")
    if args.city:
        lines.append(f"| 城市：{'、'.join(args.city)} | 省份：{'、'.join(provinces)} "
                     f"| 研招网池子按省聚合，故按省筛 |")
    elif provinces:
        lines.append(f"| 范围：{'、'.join(provinces)} | 同上 | 来自命令行或 filters.json |")
    if args.schools:
        lines.append(f"| 已有目标校：{'、'.join(args.schools)} | 直接对比，不再筛池子 | — |")
    lines.append(f"| 专业 | {'、'.join(f'{c} {n}' for c, n in majors)} | "
                 f"{'由 --major 指定' if args.major else '由本科专业映射，需你确认'} |")
    lines.append("")

    # 1. 第一层：符合条件的学校清单（学生先挑，再逐所深查）
    #    v0.3.1：旧版把「可点火几所」当成第一层，那是实现成本，不是学生的决策依据。
    #    学校名录本来就在池子里，先把选项摆出来让用户挑。
    level_req = (args.level or filters.get("level") or "").strip()
    rows = [r for r in pool if (not provinces or r.get("province") in provinces)]
    sy = [r for r in rows if "双一流" in (r.get("tag") or "")]
    others = [r for r in rows if "双一流" not in (r.get("tag") or "")]
    if level_req == "双一流":
        shown = sy
    elif level_req:
        shown = rows
    else:
        shown = rows

    # 每所学校的当前状态（已建档 / 有入口页 / 已知查不到 / 未查）
    def status_of(name: str) -> str:
        if name in ledger:
            if not majors or any(c in ledger[name]["majors"] for c, _ in majors):
                return "✅ 已查过（该专业有数据）"
            return f"🟡 已查过（已有 {'、'.join(sorted(ledger[name]['majors']))}）"
        if name in blocked:
            return "⛔ 已知查不到"
        if name in seeds:
            return "🟢 有入口页，可直接跑"
        return ""

    lines.append("## 1. 符合你条件的学校")
    lines.append("")
    if level_req == "双一流":
        lines.append(f"- 范围：{'、'.join(provinces) or '全国'}"
                     f"｜层次要求：**双一流**｜共 **{len(sy)} 所**"
                     f"（另有 {len(others)} 所非双一流，已按你的要求排除）")
    else:
        lines.append(f"- 范围：{'、'.join(provinces) or '全国'}｜层次不限"
                     f"｜共 **{len(rows)} 所**（双一流 {len(sy)} 所）")
    if not rows:
        lines.append("")
        lines.append("> 池子是空的：先跑 `python scripts/pool.py --province 北京 天津`。")
    lines.append("")
    if shown:
        cap = args.top
        head = shown[:cap]
        lines.append(f"### 可挑的学校（前 {len(head)} / {len(shown)} 所，按池子顺序，**不排名**）")
        lines.append("")
        lines.append("| # | 学校 | 省份 | 当前状态 |")
        lines.append("|---:|---|---|---|")
        for i, r in enumerate(head, 1):
            st = status_of(r["school"])
            lines.append(f"| {i} | {r['school']} | {r.get('province', '')} | {st or '—'} |")
        if len(shown) > cap:
            lines.append("")
            lines.append(f"> 还有 {len(shown) - cap} 所没列出来，用 `--top {len(shown)}` 看全。")
    lines.append("")

    # 供后面几节复用的分档
    have = [r["school"] for r in shown if r["school"] in ledger
            and (not majors or any(c in ledger[r["school"]]["majors"] for c, _ in majors))]
    can_light = [(r["school"], seeds[r["school"]]) for r in shown if r["school"] in seeds]
    cannot = [(r["school"], blocked[r["school"]]) for r in shown if r["school"] in blocked]
    partial = [(r["school"], sorted(ledger[r["school"]]["majors"])) for r in shown
               if r["school"] in ledger and r["school"] not in have]
    todo = [r["school"] for r in shown
            if r["school"] not in ledger and r["school"] not in seeds and r["school"] not in blocked]
    lines.append("")

    # 2. 第二层：风险信号（只有已建档能给）
    lines.append("## 2. 第二层：风险信号（只有已建档的能给）")
    lines.append("")
    if have:
        lines.append("| 学校 | 台账年份 | 最新复试线 | 最新均分 | 最新上线比 | 报告 |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for name in have:
            d = ledger[name]
            y = d["latest_year"]
            cur = d["years"].get(y, {})
            report = f"reports/{dt.date.today()}-{name}-{majors[0][0]}.md"
            lines.append(f"| {name} | {len(d['years'])} 年 | {cur.get('line') or '—'} "
                         f"| {cur.get('avg') or '—'} | {cur.get('online') or '—'} | {os.path.basename(report)} |")
        lines.append("")
        lines.append("> 具体的 R1~R8 判定、D×S、缺格，看各自报告——这里只列原始数字，不替你下结论。")
    else:
        lines.append("还没有任何一所跑过 `check.py`，所以现在给不出风险信号。这是正常的："
                     "**推荐的第一层永远是「先点火 3-5 所」**，不是先要一份名单。")
    lines.append("")

    # 3. 第三层：约束
    lines.append("## 3. 第三层：你的约束")
    lines.append("")
    hard = filters.get("hard_constraints") or []
    if hard:
        lines.append(f"- 硬约束（来自 filters.json）：{'；'.join(hard)}")
        lines.append("- 这些**必须逐校查**（脚本不采集钱和住）：简章「学费、住宿和校区」条款 + "
                     "校区/学费公示附表，规则见 `references/life.md`")
    else:
        lines.append("- 还没设硬约束。建议先写 `filters.json`（城市/层次/学费/宿舍），否则推荐只能按可得性排。")
    lines.append("")

    # 4. 下一步：你来挑，我去查
    lines.append("## 4. 下一步：从上面挑 3-5 所，我去查")
    lines.append("")
    lines.append("**怎么挑**（这不是排名建议，是配置建议）：")
    lines.append("")
    lines.append("- **别全挑同一档**：同一城市挑 1 所你想冲的 + 2 所你确信够得着的，比例比名单重要")
    lines.append("- **同城多校区要问清**：法学院在哪个校区，决定了你三年住哪、实习通勤多远")
    lines.append("- **优先挑你能拿到数据的**：下面这几所我现在就能跑（有入口页）")
    lines.append("")
    step = 1
    for name, (seed, tag) in can_light[:5]:
        lines.append(f"{step}. **{name}**（{tag}入口页，可直接跑）")
        lines.append("   ```powershell")
        lines.append(f"   python scripts/check.py --school {name} --major {majors[0][0]} --seed \"{seed}\"")
        lines.append("   ```")
        step += 1
    lines.append(f"{step}. **你挑的那几所**：告诉我校名，我去找入口页并跑；"
                 f"目录如果是动态查询系统（抓不到结构化表），我会停下来找你要链接")
    lines.append("")
    lines.append("> **我查不到时会怎么做**：不猜、不编，直接给你具体的检索式和填空格式，"
                 "你贴回来一次，之后就一直记住。")
    if not rows:
        lines.append("")
        lines.append("> 先建池子：`python scripts/pool.py --province 北京 天津`")
    lines.append("")

    lines.append("---")
    lines.append("> 这份推荐回答的是「下一步该查哪几所」，不是「哪几所更好」。"
                 "后者需要你没有的数据（全国可比的学科质量），所以本工具不提供。")

    md = "\n".join(lines)
    os.makedirs(REPORTS, exist_ok=True)
    out = args.out or os.path.join(REPORTS, f"{dt.date.today()}-推荐.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md.split("## 2.")[0])
    print(f"→ {os.path.relpath(out, DATA_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

