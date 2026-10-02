# 计费口径与换算（已验证）

## NewAPI

quota 就按这个公式算（实测比值中位数 0.999949，90% ≤1%）：

```
quota = (prompt - cache)×model_ratio×group_ratio
      + cache×cache_ratio×model_ratio×group_ratio
      + completion×completion_ratio×model_ratio×group_ratio
```

### ★ quota → 金额 的换算系数是「站点自定义」的

**这是头号坑。** NewAPI 默认 `QuotaPerUnit=500000`，但每个站可不同。

口径（2026-10-01 用户拍板）：**1 美元 = 1 人民币**，所以最终金额直接由
`quota ÷ quota_per_cny` 得到「元」，不再二次乘汇率。

配置位置：`sites.json` 的 `quota_per_cny`（每站可配），默认 500000。

### ⚠️ 历史口径更正（2026-10-01 实测复核）

本技能曾记「d1api.xin 系数 = 2,600,000，不是默认 500000」—— **该说法已作废**。

2026-10-01 用库内数据复核，**d1api.xin 的系数就是 500000**，精确自洽：

```
2026-09-21  quota=66637    cost=0.133274   quota/500000=0.133274  比值=1.0000
2026-09-29  quota=7519     cost=0.015038   quota/500000=0.015038  比值=1.0000
2026-09-30  quota=30666    cost=0.061332   quota/500000=0.061332  比值=1.0000
2026-10-01  quota=350361   cost=0.700722   quota/500000=0.700722  比值=1.0000
```

2,600,000 是**早期历史口径**（当时还没统一换算方式），勿再沿用。

### 换站怎么校准

```
系数 = 当日 quota 合计 ÷ (站方网页显示金额 ÷ 汇率)
```

在 `sites.json` 用 `quota_per_cny` 配（每站可配），改配置不改代码。
默认 500000。

⚠️ 历史字段名：曾用 `quota_per_usd`（当时按「美元系数」设计、再由汇率换算）；
现统一为 `quota_per_cny`（直接得元）。旧配置里见到 `quota_per_usd` 要改成新名。

辅助脚本（已归档到 `archive/探索与排查/`）：`verify_quota_formula.py`（验算公式）、
`recalibrate_coef.py`（重校准系数）。需要时搬回主目录即可跑。

## sub2api

字段直接给标价与实付，**不需要任何换算**：

| 字段 | 含义 |
|---|---|
| `cost` | 标价 |
| `actual_cost` | **实付**（= cost × 倍率，实测精确 0.08）|
| `account_cost` | 上游成本 |

倍率从 `/v1/sub2api/billing` 的 `effective_rate_multiplier` 取，自动校准。

### ⚠️ 同类型 ≠ 同参数（2026-10-01 实测）

同为 sub2api 的两个站，倍率可以差很多：

```
tryaigc   倍率 7
dshapi    倍率 0.08    （差 87 倍）
```

所以**加站后必须实测该站的倍率**，不能照搬。

## 口径换算式

```
站方 prompt_tokens（含缓存） ≈ Hermes input_tokens + Hermes cache_read_tokens
站方 quota ÷ quota_per_cny = 金额
```

09-19 实测：站方 26,144,384 vs 本地 26,144,768 → **差 0.0015%**，公式成立。
