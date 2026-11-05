# data/schools.csv 的字段与校验规则

台账的作用是**让每一年只查增量**。所以它按「一行 = 一个年份 + 一个同口径对象」组织，追加式写入，不覆盖。

## 字段

| 列名 | 含义 | 取值规则 |
|---|---|---|
| `school` | 学校 | 全称，不加「大学/学院」以外的修饰 |
| `college` | 学院 | 招生单位全称 |
| `major_code` | 专业代码 | 六位数字，如 `085404` |
| `major_name` | 专业名称 | 与专业目录一致 |
| `degree_type` | 培养方式 | `学硕` / `专硕` |
| `study_mode` | 学习方式 | `全日制` / `非全` |
| `enroll_year` | 入学年份 | 四位数字（比考试年份 +1） |
| `plan_total` | 计划招生总数 | 专业目录「拟招生人数」原文数字 |
| `plan_exempt` | 推免拟接收 | 推免公示数字 |
| `plan_special` | 专项计划 | 少干、退役大学生等，没有写 `0` |
| `quota_exam` | 统考名额 | `plan_total − plan_exempt − plan_special`；缺任一项就写 `unknown`，**不许硬减** |
| `line_school` | 校线 | 整数，没有写 `unknown` |
| `line_college` | 院线 | 整数，没有写 `unknown` |
| `line_major` | 专业线 / 实际复试线 | 整数，没有写 `unknown` |
| `admit_min` | 录取最低分 | 来自拟录取名单，整数 |
| `admit_avg` | 录取均分 | 整数（自己算的要在 `note` 里注明算法） |
| `admit_median` | 录取中位数 | 整数，优先用这个做 Δ 的分母 |
| `fc_pass_rate` | 一志愿上线率 = 上线 ÷ 统考录取 | **可 > 1**（`1.8` 表示上线人数接近名额两倍）；不公示写 `unknown`。**不是**「录取率」，后者只写 `note` |
| `retest_ratio` | 复试差额比例 | 如 `1.2`（120%） |
| `retest_weight` | 复试成绩占比 | 0-1 小数，如 `0.4` |
| `subject_codes` | 初试科目代码 | 如 `101;204;302;408`，用分号分隔 |
| `changed_exam` | 是否改考 | `0` / `1`，`1` 时在 `note` 写清改了什么 |
| `sources` | 来源 | `名称\|URL\|发布日期\|访问日期`，多条用 `；` 分隔 |
| `note` | 备注 | 口径差异、数据异常、推算过程 |
| `campus` | 院系所在校区 | 院系口径，不是学校所在地；多校区必须写具体校区名 |
| `tuition` | 学费 | 元/生·学年，纯数字；非全、中外合作单独标注 |
| `dorm_available` | 是否提供校内住宿 | `提供` / `不提供` / `仅学硕` / `unknown` |
| `dorm_fee` | 住宿费 | 元/生·学年或官方区间原样抄（如 `800-1200（仅适用可申请者）`），不取中位数 |

**生活侧四列的来源固定为 L1**：简章正文（学费/住宿/校区条款）、各校独立的《校区列表》《学费收费公示表》。**L4 的宿舍体验不进台账** —— 台账只收可复核的数字与政策条文，在校生说法留在报告正文里（规则见 `life.md`）。

## 写入规则

1. **空值写 `unknown`，不要写空白，更不要写 `0`。** 空白和 0 会被后续计算当成真值，`unknown` 不会。
2. **追加不覆盖。** 旧行错了，新增一行并在 `note` 写 `corrected_from: <旧行的 enroll_year>`。
3. **一行只装一个口径。** 学硕和专硕、全日制和非全、不同专业代码，都不许挤在一行。
4. **同一 `school + major_code + degree_type + study_mode + enroll_year` 只能有一行**（除非是修正行）。
5. 数字单位固定：分数为整数，人数为整数，比率为 0-1 小数。

## 校验清单（写完自查）

- [ ] `quota_exam ≤ plan_total`，除非写了 unknown
- [ ] `admit_min ≤ admit_avg`（若两者都有）
- [ ] 同一对象同一年没有两行
- [ ] `sources` 里每条都有四条信息（名称 / URL / 发布日期 / 访问日期）
- [ ] 有 `admit_avg` 但没有 `line_*` 的行，置信度要降级
- [ ] `changed_exam = 1` 的行，`note` 里说清了改什么

## 示例行（全部为占位，禁止当作真实数据引用）

```csv
school,college,major_code,major_name,degree_type,study_mode,enroll_year,plan_total,plan_exempt,plan_special,quota_exam,line_school,line_college,line_major,admit_min,admit_avg,admit_median,fc_pass_rate,retest_ratio,retest_weight,subject_codes,changed_exam,sources,note
示例大学,示例学院,000000,示例专业,专硕,全日制,2027,unknown,unknown,0,unknown,unknown,unknown,unknown,unknown,unknown,unknown,unknown,unknown,unknown,101;204;302;000,0,示例来源|https://example.edu.cn/notice|2026-09-15|2026-11-01,这是结构示例，所有数值均为 unknown，用于演示字段顺序
```

**为什么示例行也全是 unknown：** 编一个看起来合理的假分数，比留空危险得多 —— 它会被后面某次阅读当成真数据。空着永远比编造诚实。
