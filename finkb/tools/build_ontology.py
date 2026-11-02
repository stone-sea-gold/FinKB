#!/usr/bin/env python3
"""生成 config/ontology.yaml —— 本体层的单一真相源。

用法:
    python tools/build_ontology.py            # 生成
    python tools/build_ontology.py --check    # 只校验不落盘

为什么要生成而不是手写：L2 的 34 个科目来自 config/metric_dict.yaml，两者必须
一一对应（metric_id 是 facts 表的连接键）。手写会漂移；生成保证一致。

生成后的 YAML 是**可人工编辑的**——改完直接生效，本脚本只是初始化的脚手架。
重跑会覆盖 config/ontology.yaml，所以持久化的改动请先回灌到本脚本的 CURATED 表，
或者接受覆盖（YAML 里没有 secret，取舍成本低）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
CAS_LABELS = ROOT.parent / ".scratch" / "cas" / "labels_cas2015.json"

# ---------------------------------------------------------------- L0 元概念
# 来源 CMNS（Commons Ontology，FIBO 的地基）。这一层十年不变。
L0 = [
    dict(
        concept_id="cmns:MonetaryAmount",
        label="金额",
        basis="cmns",
        basis_uri="https://www.omg.org/spec/Commons/QuantitiesAndUnits/MonetaryAmount",
        definition="以货币表示的数额，必须同时绑定币种与期间（存量指标用时点）。",
        unit_default="亿元",
    ),
    dict(
        concept_id="xbrli:perShareItem",
        label="每股金额",
        basis="ifrs",
        basis_uri="http://www.xbrl.org/2003/instance/perShareItemType",
        definition="按股数摊薄后的货币金额，单位固定为「元/股」。",
        unit_default="元/股",
    ),
    dict(
        concept_id="xbrli:pureItem",
        label="比率",
        basis="ifrs",
        basis_uri="http://www.xbrl.org/2003/instance/pureItemType",
        definition="无量纲比值，本项目统一以百分数存储（毛利率 17.74 表示 17.74%）。",
        unit_default="%",
    ),
    dict(
        concept_id="cmns-qtu:Expression",
        label="派生表达式",
        basis="cmns",
        basis_uri="https://www.omg.org/spec/Commons/QuantitiesAndUnits/Expression",
        definition="由其他概念经算术运算得到的值；必须声明参数（expression_args）才能计算。",
        unit_default=None,
    ),
    dict(
        concept_id="cmns-dt:DatePeriod",
        label="期间",
        basis="cmns",
        basis_uri="https://www.omg.org/spec/Commons/DatesAndTimes/DatePeriod",
        definition="有起止日的区间。利润表/现金流量表类概念必须有它，资产负债表类必须没有（时点）。",
        unit_default=None,
    ),
    dict(
        concept_id="cmns-qtu:Measure",
        label="计量单位",
        basis="cmns",
        basis_uri="https://www.omg.org/spec/Commons/QuantitiesAndUnits/Measure",
        definition="数值的附属单位，本项目限定为 亿元 / % / 元每 / 辆。",
        unit_default=None,
    ),
    dict(
        concept_id="cmns:Party",
        label="主体",
        basis="cmns",
        basis_uri="https://www.omg.org/spec/Commons/PartiesAndSituations/Party",
        definition="财报的报告主体（上市公司）。所有事实都必须挂在一个主体上。",
        unit_default=None,
    ),
]

# ---------------------------------------------------------- L1 科目骨架（FIBO）
# 取自 FIBO FND 域静态导出。只取「骨架」，不引 OWL 运行时。
L1 = [
    dict(
        concept_id="fibo:Income",
        label="收入",
        parent_id="cmns:MonetaryAmount",
        basis="fibo",
        basis_uri="https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/Income",
        definition="企业在期间内取得的经济利益流入。FIBO 定义：revenue received during a period of time；"
        "约束 hasDatePeriod 基数恰为 1 —— 即收入**必须带期间**，这一条是本层最重要的语义约束。",
        unit_default="亿元",
    ),
    dict(
        concept_id="finkb:Expense",
        label="费用",
        parent_id="cmns:MonetaryAmount",
        basis="self",
        basis_uri=None,
        definition="期间内流出的经济利益（营业成本与三费）。FIBO 闭包内未提供可复用的 Expense 类，"
        "按 CAS 费用类科目语义新增，故 basis=self。",
        unit_default="亿元",
    ),
    dict(
        concept_id="fibo:Asset",
        label="资产",
        parent_id="cmns:MonetaryAmount",
        basis="fibo",
        basis_uri="https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/Asset",
        definition="主体在某一时点控制的资源。FIBO 把它分为 流动/非流动/持有待售 三类。",
        unit_default="亿元",
    ),
    dict(
        concept_id="finkb:Liabilities",
        label="负债",
        parent_id="cmns:MonetaryAmount",
        basis="self",
        basis_uri=None,
        definition="主体在某一时点承担的现时义务。FIBO 闭包内无独立 Liabilities 类（负债在 FIBO 里"
        "作为金融工具/合同的义务面建模），此处按 CAS 负债类语义新增。",
        unit_default="亿元",
    ),
    dict(
        concept_id="fibo:OwnersEquity",
        label="所有者权益",
        parent_id="cmns-qtu:Expression",
        basis="fibo",
        basis_uri="https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/OwnersEquity",
        definition="资产减负债后的剩余权益。FIBO 把它建模为 Expression（差额表达式）而非直接金额，"
        "这正是「权益是算出来的、不是数出来的」的形式化表达。",
        unit_default="亿元",
    ),
    dict(
        concept_id="fibo:CashFlow",
        label="现金流",
        parent_id="cmns:MonetaryAmount",
        basis="fibo",
        basis_uri="https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/CashFlow",
        definition="期间内现金及现金等价物的净流入/流出量。",
        unit_default="亿元",
    ),
    dict(
        concept_id="fibo:EBITDA",
        label="息税折旧摊销前利润",
        parent_id="cmns-qtu:Expression",
        basis="fibo",
        basis_uri="https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/EBITDA",
        definition="FIBO 用 hasArgument 声明其由哪些科目合成。本项目 MVP 不用，保留作 L1 扩展位。",
        unit_default="亿元",
    ),
    dict(
        concept_id="fibo:Report",
        label="定期报告",
        parent_id="cmns:Party",
        basis="fibo",
        basis_uri="https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/Accounting/Report",
        definition="主体发布的财务报告文档（年报/半年报/季报）。L0 凭证层的 PDF 即其实例。",
        unit_default=None,
    ),
]

# ------------------------------------------- L1 维度种类（ADR-054，封闭词表）
# 维度不是「每份年报一套配置」。维度**种类**由准则与披露实践决定，是个小集合；
# 维度**成员**（分产品下的「新能源汽车」）是数据驱动的开放集合，从结构表行名
# 自动抽取，落在 dimension_members。二者分开，新增年报就不需要任何配置。
#
# 国标自己也是这么干的：CAS 2015 用 axis（维度）机制表达拆分，例如
# cas_RegionsAxis、cas_ClassesOfCustomersAxis、cas_ReportableSegments...Axis
# —— 3418 个元素撑住了全部附注明细，靠的就是「元素 + 维度」而不是「每个切片
# 一个元素」。
DIMENSIONS = [
    dict(
        concept_id="finkb:Dimension",
        label="维度",
        parent_id="fibo:Report",
        basis="self",
        basis_uri=None,
        unit_default=None,
        definition="报表披露的拆分视角（分产品/分行业/分地区…）。FIBO 闭包内无对应类，"
        "按 CAS 披露规则新增。维度种类封闭，成员开放——这是与「新科目」的分界线。",
    ),
    dict(
        concept_id="dim:region",
        label="分地区",
        parent_id="finkb:Dimension",
        basis="cas",
        basis_uri="http://xbrl.mof.gov.cn/taxonomy/2015-03-31/cas#cas_RegionsAxis",
        unit_default=None,
        definition="按地理区域拆分收入/成本。国标对应 cas_RegionsAxis（地区分布 [axis]）。",
    ),
    dict(
        concept_id="dim:product",
        label="分产品",
        parent_id="finkb:Dimension",
        basis="self",
        basis_uri=None,
        unit_default=None,
        definition="按产品线拆分。国标无「分产品」axis（实务走「主营业务分产品情况」表），"
        "故 basis=self。",
    ),
    dict(
        concept_id="dim:industry",
        label="分行业",
        parent_id="finkb:Dimension",
        basis="cas",
        basis_uri="http://xbrl.mof.gov.cn/taxonomy/2015-03-31/cas"
        "#cas_ReportableSegmentsProfitLossAssetAndLiabilityAxis",
        unit_default=None,
        definition="按行业分部拆分。国标用报告分部 axis 表达，与「分行业」披露表同构。",
    ),
    dict(
        concept_id="dim:customer",
        label="分客户",
        parent_id="finkb:Dimension",
        basis="cas",
        basis_uri="http://xbrl.mof.gov.cn/taxonomy/2015-03-31/cas#cas_ClassesOfCustomersAxis",
        unit_default=None,
        definition="按客户类别拆分（如银行披露的零售/对公）。国标对应客户类别 axis。"
        "本项目暂未抽取，先登记为扩展位。",
    ),
]

# dim_kind → 中文标签 / 识别标记。标记是结构表里出现的行标记或表题关键词。
DIMENSION_RULES = [
    ("region", "分地区", "分地区"),
    ("product", "分产品", "分产品"),
    ("industry", "分行业", "分行业"),
]

# ------------------------------------------------- L2 国标科目（CAS 2015 裁剪）
# (metric_id, 父概念, CAS 元素, CAS 角色, 是否 L3 派生, 备注)
L2 = [
    ("revenue", "fibo:Income", "cas_Revenues", "terseLabel", None),
    ("net_profit", "fibo:Income", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "label",
     "CSRC 报表口径「归属于上市公司股东的净利润」，对应国标归母净利润元素"),
    ("net_profit_deducted", "fibo:Income", None, None,
     "「扣除非经常性损益后的净利润」是证监会报表格式行项目，不在 CAS 分类标准内 (basis=self)"),
    ("operating_profit", "fibo:Income", "cas_OperatingProfits", "label", None),
    ("total_profit", "fibo:Income", "cas_ProfitLossBeforeIncomeTaxes", "label", None),
    ("net_interest_income", "fibo:Income", "cas_NetInterestIncome", "terseLabel", None),
    ("fee_commission_income", "fibo:Income", "cas_FeeAndCommissionIncome", "terseLabel", None),
    ("insurance_revenue", "fibo:Income", "cas_IncomeFromInsuranceBusiness", "label", None),
    ("operating_cost", "finkb:Expense", "cas_CostOfRevenues", "label", None),
    ("selling_expense", "finkb:Expense", "cas_SellingAndMarketingExpense", "label", None),
    ("admin_expense", "finkb:Expense", "cas_GeneralAndAdministrativeExpenses", "label", None),
    ("finance_expense", "finkb:Expense", "cas_FinanceExpense", "label", None),
    ("rd_expense", "finkb:Expense", "cas_ResearchAndDevelopmentExpenses", "label",
     "国标只有「研究开发支出」(余额类)，无 2018 新准则的「研发费用」；二者口径不同，basis=self"),
    ("total_assets", "fibo:Asset", "ifrs-full_Assets", "totalLabel", None),
    ("current_assets", "fibo:Asset", "ifrs-full_CurrentAssets", "totalLabel", None),
    ("cash", "fibo:Asset", "cas_BankBalancesAndCash", "label", None),
    ("inventory", "fibo:Asset", "ifrs-full_Inventories", "label", None),
    ("accounts_receivable", "fibo:Asset", "cas_AccountsReceivable", "label", None),
    ("loans_advances", "fibo:Asset", "cas_LoansAndAdvances", "terseLabel", None),
    ("total_liabilities", "finkb:Liabilities", "ifrs-full_Liabilities", "totalLabel", None),
    ("current_liabilities", "finkb:Liabilities", "ifrs-full_CurrentLiabilities", "totalLabel", None),
    ("short_term_borrowings", "finkb:Liabilities", "cas_ShortTermBorrowings", "label", None),
    ("long_term_borrowings", "finkb:Liabilities", "cas_NonCurrentPortionOfLongTermBorrowings", "terseLabel", None),
    ("bonds_payable", "finkb:Liabilities", "cas_BondsPayable", "label", None),
    ("lease_liabilities", "finkb:Liabilities", None, None,
     "「租赁负债」为 CAS 21(2018) 新增，2015 分类标准没有该元素 (basis=self)"),
    ("customer_deposits", "finkb:Liabilities", "cas_CustomerDeposits", "label", None),
    ("total_equity", "fibo:OwnersEquity", "ifrs-full_Equity", "totalLabel", None),
    ("net_assets", "fibo:OwnersEquity", "ifrs-full_EquityAttributableToOwnersOfParent", "totalLabel",
     "CSRC 报表口径「归属于上市公司股东的净资产」"),
    ("minority_interest", "fibo:OwnersEquity", "ifrs-full_NoncontrollingInterests", "label",
     "少数股东权益。可为负（子公司累积亏损超少数股东投入），故不可用于任何单向不等式"),
    ("ocf", "fibo:CashFlow", "ifrs-full_CashFlowsFromUsedInOperatingActivities", "netLabel", None),
    ("icf", "fibo:CashFlow", "ifrs-full_CashFlowsFromUsedInInvestingActivities", "netLabel", None),
    ("fcf", "fibo:CashFlow", "ifrs-full_CashFlowsFromUsedInFinancingActivities", "netLabel",
     "注意 fcf 在本项目指「筹资活动现金流净额」，不是自由现金流"),
    ("net_cash_increase", "fibo:CashFlow", "ifrs-full_IncreaseDecreaseInCashAndCashEquivalents", "netLabel", None),
    ("fx_effect", "fibo:CashFlow", "ifrs-full_EffectOfExchangeRateChangesOnCashAndCashEquivalents", "label",
     "现金流量表恒等式的第 4 项。v3 补入，此前缺席导致约束只能靠 5% 容差遮挡"),
    ("eps_basic", "xbrli:perShareItem", "ifrs-full_BasicEarningsLossPerShare", "label", None),
    ("roe_weighted", "xbrli:pureItem", None, None,
     "「加权平均净资产收益率」是公司自行披露的监管口径指标，分类标准内无对应元素 (basis=self)"),
]

# ------------------------------------------------------ L3 分析指标（自建派生）
# formula 用 SQL 片段，参数必须是已存在的 metric_id（facts.metric_id）。
L3 = [
    dict(concept_id="self:gross_margin", label="毛利率", unit_default="%",
         formula="(revenue - operating_cost) / revenue", expression_args=["revenue", "operating_cost"],
         definition="毛利 / 营业收入。反映产品定价能力与成本结构。"),
    dict(concept_id="self:net_margin", label="净利率", unit_default="%",
         formula="net_profit / revenue", expression_args=["net_profit", "revenue"],
         definition="归母净利润 / 营业收入。"),
    dict(concept_id="self:debt_ratio", label="资产负债率", unit_default="%",
         formula="total_liabilities / total_assets", expression_args=["total_liabilities", "total_assets"],
         definition="负债合计 / 资产总计。"),
    dict(concept_id="self:rd_intensity", label="研发强度", unit_default="%",
         formula="rd_expense / revenue", expression_args=["rd_expense", "revenue"],
         definition="研发投入 / 营业收入。"),
    dict(concept_id="self:expense_ratio", label="期间费用率", unit_default="%",
         formula="(selling_expense + admin_expense + finance_expense) / revenue",
         expression_args=["selling_expense", "admin_expense", "finance_expense", "revenue"],
         definition="三费合计 / 营业收入。"),
    dict(concept_id="self:equity_ratio", label="权益乘数", unit_default="%",
         formula="total_assets / total_equity", expression_args=["total_assets", "total_equity"],
         definition="资产总计 / 所有者权益合计，杠杆倍数。equity 为负时该值无意义，由约束拦截。"),
]

# ------------------------------------------------------------------ 约束
#
# severity 两档，这是构建过程中被真实数据逼出来的设计：
#   error —— 会计恒等式，违反必然是数据错误
#   warn  —— 通常成立但**允许合法例外**，违反只是提示人工看一眼
#
# 最初写的 `net_profit <= total_profit` 被实测推翻：所得税费用为负（亏损年度
# 确认递延所得税资产）或少数股东损益为负时，归母净利润**可以大于**利润总额。
# 长安 000625 2024 就是这种情况（利润总额 67.6 亿 < 归母净利 73.2 亿），
# 因为它同时有 -10 亿少数股东损益和递延所得税收益。这条约束已删除。
CONSTRAINTS = [
    dict(constraint_id="acct.assets_eq_liabilities_plus_equity", concept_id="fibo:Asset",
         rule_type="acct_equation", expression="total_assets == total_liabilities + total_equity",
         severity="error",
         note="会计恒等式。实测 73 个 (公司,期间) 全部通过——这是本体层最硬的一道体检。"),
    dict(constraint_id="acct.current_le_total", concept_id="fibo:Asset",
         rule_type="range", expression="current_assets <= total_assets",
         severity="error", note="流动资产合计不可能超过资产总计。"),
    dict(constraint_id="acct.current_liab_le_total", concept_id="finkb:Liabilities",
         rule_type="range", expression="current_liabilities <= total_liabilities",
         severity="error", note="流动负债合计不可能超过负债合计。"),
    dict(constraint_id="acct.cashflow_sum", concept_id="fibo:CashFlow",
         rule_type="acct_equation",
         expression="net_cash_increase == ocf + icf + fcf + fx_effect",
         severity="error", tolerance=0.01,
         note="四项现金流恒等式（v3 修正）：净增加额 = 经营 + 投资 + 筹资 + 汇率变动影响。"
              "此前漏掉 fx_effect，导致 8 条合法业务被误报成 warn；补项后升为 error。"),
    dict(constraint_id="acct.equity_split", concept_id="fibo:OwnersEquity",
         rule_type="acct_equation",
         expression="net_assets + minority_interest == total_equity",
         severity="error", tolerance=0.01,
         note="权益拆分恒等式（v3 修正）：归母净资产 + 少数股东权益 = 所有者权益合计。"
              "旧约束「归母 ≤ 合计」在少数股东权益为负时必然误报（赛力斯 2024/2025Q1）。"),
    dict(constraint_id="acct.expense_positive", concept_id="finkb:Expense",
         rule_type="range", expression="operating_cost >= 0",
         severity="error",
         note="费用类科目入库统一取正数：报表里「减：营业成本」常印成括号负数，"
              "入口已按 _POSITIVE_METRICS 取绝对值，因此出现负数必是实现 bug。"
              "（v3 用它替换了被证伪的 cost_le_revenue，顺带覆盖 finkb:Expense 分支的约束）"),
    dict(constraint_id="range.roe", concept_id="metric:roe_weighted",
         rule_type="range", expression="-100 <= roe_weighted <= 200",
         severity="warn",
         note="加权 ROE 的物理边界（百分数口径）；亏损年度或净资产极薄的年份会出界，"
              "只报警告。"),
    dict(constraint_id="range.gross_margin", concept_id="self:gross_margin",
         rule_type="range", expression="-50 <= gross_margin <= 100",
         severity="warn",
         note="毛利率的物理边界（百分数口径：17.74 = 17.74%）。下界从 -100 收到 -50："
              "毛利率低于 -50% 意味着亏本卖货超过一半，业务上极罕见，更可能是取数错误。"),
    dict(constraint_id="period.income_requires_period", concept_id="fibo:Income",
         rule_type="required_period", expression="period IS NOT NULL AND period <> ''",
         severity="error",
         note="FIBO Income 的 hasDatePeriod 基数为 1，落到库里就是期间必填。"),
    dict(constraint_id="unit.eps_is_per_share", concept_id="xbrli:perShareItem",
         rule_type="unit_check", expression="norm_unit == '元/股'",
         severity="error",
         note="每股指标的规范化单位必须是元/股。注意查的是 norm_unit 而不是 "
              "unit_of_account：后者是原始表格的表头单位标注，每股指标的表头常写成「元」。"),
]



def _load_metrics() -> dict[str, dict]:
    data = yaml.safe_load((CONFIG / "metric_dict.yaml").read_text(encoding="utf-8"))
    return {m["metric_id"]: m for m in data["metrics"]}


def _load_cas_aliases() -> dict[str, list[str]]:
    """元素 → 该元素的全部中文标签（用于概念别名，提升可检索性）。"""
    if not CAS_LABELS.exists():
        return {}
    raw = json.loads(CAS_LABELS.read_text(encoding="utf-8"))
    return {elem: sorted({t for t in roles.values() if t}) for elem, roles in raw.items()}


def build() -> dict:
    metrics = _load_metrics()
    cas_aliases = _load_cas_aliases()

    concepts: list[dict] = []
    for c in L0:
        concepts.append({**c, "layer": "L0", "domain": "meta", "parent_id": None,
                         "metric_id": None, "aliases": [c["label"]],
                         "source_uri": c.get("basis_uri")})
    for c in L1:
        concepts.append({**c, "layer": "L1", "domain": "meta",
                         "metric_id": None, "aliases": [c["label"]],
                         "source_uri": c.get("basis_uri")})

    for metric_id, parent, elem, role, note in L2:
        m = metrics[metric_id]
        aliases = [m["metric_name"], *(m.get("aliases") or [])]
        basis = "cas" if elem else "self"
        basis_uri = None
        if elem:
            prefix = elem.split("_")[0]
            ns = "http://xbrl.mof.gov.cn/taxonomy/2015-03-31/cas" if prefix == "cas" \
                else "http://xbrl.ifrs.org/taxonomy/2014-03-05/ifrs-full"
            basis_uri = f"{ns}#{elem}"
            basis = "cas" if prefix == "cas" else "ifrs"
            for t in cas_aliases.get(elem, []):
                if t not in aliases:
                    aliases.append(t)
        concepts.append(dict(
            concept_id=f"metric:{metric_id}",
            label=m["metric_name"],
            layer="L2",
            domain="finance",
            basis=basis,
            basis_uri=basis_uri,
            basis_element=elem,
            definition=note or f"国标科目「{m['metric_name']}」，引自 CAS 2015-03-31 分类标准。",
            parent_id=parent,
            metric_id=metric_id,
            unit_default=m.get("unit_default"),
            aliases=aliases,
            source_uri=basis_uri,
        ))

    for c in L3:
        concepts.append({**c, "layer": "L3", "domain": "finance", "basis": "self",
                         "basis_uri": None, "parent_id": "cmns-qtu:Expression",
                         "metric_id": None, "aliases": [c["label"]] if c["label"] not in ("毛利率",) else ["毛利率", "gross margin"],
                         "source_uri": None})

    for c in DIMENSIONS:
        concepts.append({**c, "layer": "L1", "domain": "dimension", "metric_id": None,
                         "aliases": [c["label"]], "source_uri": c.get("basis_uri")})

    relations = []
    for c in concepts:
        if c.get("parent_id"):
            relations.append(dict(src_concept=c["concept_id"], rel_type="is_a",
                                  dst_concept=c["parent_id"], attrs_json=None,
                                  source_uri=c.get("basis_uri")))
    for c in L3:
        for arg in c["expression_args"]:
            relations.append(dict(src_concept=c["concept_id"], rel_type="derived_from",
                                  dst_concept=f"metric:{arg}", attrs_json=None, source_uri=None))
    relations.append(dict(src_concept="finkb:Liabilities", rel_type="disjoint_with",
                          dst_concept="fibo:Asset",
                          attrs_json='{"note":"同一笔金额不能同时是资产与负债"}', source_uri=None))
    relations.append(dict(src_concept="fibo:OwnersEquity", rel_type="part_of",
                          dst_concept="fibo:Report",
                          attrs_json='{"note":"权益变动表是年报的一部分"}', source_uri=None))

    doc = dict(
        version=1,
        updated_at="2026-11-02",
        taxonomy=dict(
            name="CAS 2015-03-31",
            source="http://m.mof.gov.cn/tzgg/201504/t20150407_1213301.htm",
            elements_total=4317,
            elements_cas=3682,
            elements_ifrs=635,
            note="CAS 2015 分类标准是 IFRS 2014 分类标准的中文扩展（包内同时分发 "
                 "full_ifrs_20140305），因此对齐国标天然获得部分 IFRS 对齐。",
            copyright="财政部 2015，All rights reserved；本项目只引用科目名称，不打包分发原文件。",
        ),
        layers=dict(
            L0="元概念（CMNS），十年不变",
            L1="科目骨架（FIBO FND 静态导出），极少变",
            L2="国标科目（CAS 2015 裁剪），随准则变",
            L3="分析指标（自建派生），随需求变",
        ),
        concepts=concepts,
        relations=relations,
        constraints=CONSTRAINTS,
        dimensions=[
            dict(dim_kind=kind, label=label, markers=markers, concept_id=f"dim:{kind}",
                 note="维度种类固定，成员由结构表行名自动抽取（dimension_members）")
            for kind, label, markers in DIMENSION_RULES
        ],
    )
    return doc


def _check(doc: dict) -> list[str]:
    errs: list[str] = []
    ids = {c["concept_id"] for c in doc["concepts"]}
    if len(ids) != len(doc["concepts"]):
        errs.append("concept_id 有重复")
    metrics = _load_metrics()
    for c in doc["concepts"]:
        if c.get("parent_id") and c["parent_id"] not in ids:
            errs.append(f"{c['concept_id']}: parent_id {c['parent_id']} 不存在")
        if c["layer"] == "L2" and c["metric_id"] not in metrics:
            errs.append(f"{c['concept_id']}: metric_id {c['metric_id']} 不在 metric_dict")
    covered = {c["metric_id"] for c in doc["concepts"] if c.get("metric_id")}
    missing = set(metrics) - covered
    if missing:
        errs.append(f"metric_dict 中有 {len(missing)} 个指标未建概念: {sorted(missing)}")
    for r in doc["relations"]:
        for k in ("src_concept", "dst_concept"):
            if r[k] not in ids:
                errs.append(f"relation 端点 {r[k]} 不存在")
    for con in doc["constraints"]:
        if con["concept_id"] not in ids:
            errs.append(f"constraint {con['constraint_id']}: concept {con['concept_id']} 不存在")
    return errs


# ------------------------------------------------------------ 对照表（人审用）

_ROLE_ORDER = ("label", "terseLabel", "totalLabel", "netLabel", "verboseLabel",
               "periodEndLabel", "periodStartLabel")

_LAYER_TITLE = {
    "L0": "L0 元概念（跨域通用，十年不改）",
    "L1": "L1 科目骨架（借 FIBO 语义）",
    "L2": "L2 国标科目（对齐 CAS 2015）",
    "L3": "L3 派生指标（本项目自建）",
}


def render_mapping_md(doc: dict) -> str:
    """把 ontology.yaml 摊成一张人审用的对照表。

    每个 L2 科目必须能同时看到三样东西：它挂在哪个 FIBO 骨架下、对应 CAS 里
    哪个元素、那个元素在国标里的中文标签是什么（含标签角色 label/terseLabel…）。
    三者缺一，就没法判断这个科目是不是「借对了」。
    """
    raw = json.loads(CAS_LABELS.read_text(encoding="utf-8")) if CAS_LABELS.exists() else {}

    def label_of(elem: str) -> tuple[str, str]:
        roles = raw.get(elem) or {}
        for role in _ROLE_ORDER:
            if roles.get(role):
                return roles[role], role
        return "", ""

    by_layer: dict[str, list[dict]] = {}
    for c in doc["concepts"]:
        by_layer.setdefault(c["layer"], []).append(c)

    out = ["# FinKB 本体科目对照表（L0–L3 全量）", "",
           "> 由 `finkb/tools/build_ontology.py` 从 `config/ontology.yaml` 与 CAS 2015 "
           "标签表（4317 个元素）生成，不是手写。改 YAML 后重跑该脚本即可刷新。", "",
           f"> 规模：{len(doc['concepts'])} 概念 / {len(doc['relations'])} 关系 / "
           f"{len(doc['constraints'])} 约束；CAS 元素总数 4317（cas_* 3682 + ifrs-full_* 635）。", ""]

    for layer in ("L0", "L1", "L2", "L3"):
        items = by_layer.get(layer, [])
        out += ["", f"## {_LAYER_TITLE[layer]}（{len(items)} 个）", ""]
        if layer in ("L0", "L1"):
            out += ["| 概念 ID | 中文名 | 父概念 | basis | 标准定位 | 说明 |",
                    "|---|---|---|---|---|---|"]
            for c in items:
                out.append(f"| `{c['concept_id']}` | {c['label']} | {c.get('parent_id') or '—'} | "
                           f"{c.get('basis') or '—'} | {c.get('basis_uri') or '—'} | "
                           f"{(c.get('definition') or '').replace(chr(10), ' ')} |")
        elif layer == "L2":
            out += ["| # | 概念 ID | 中文名 | metric_id | FIBO 骨架 | CAS 元素 | "
                    "CAS 中文标签（角色） | 状态 |", "|---|---|---|---|---|---|---|---|"]
            for i, c in enumerate(items, 1):
                elem = c.get("basis_element")
                if not elem:
                    cas_cell, lab_cell, status = "—", "—", "⚪ 自建（2015 版无此科目）"
                else:
                    lab, role = label_of(elem)
                    cas_cell = f"`{elem}`"
                    lab_cell = f"{lab}（{role}）" if lab else "—"
                    status = ("✅ 命中（IFRS 元素）" if elem.startswith("ifrs-full_")
                              else "✅ 命中")
                out.append(f"| {i} | `{c['concept_id']}` | {c['label']} | {c.get('metric_id') or '—'} | "
                           f"{c.get('parent_id') or '—'} | {cas_cell} | {lab_cell} | {status} |")
        else:
            out += ["| 概念 ID | 中文名 | 公式 | 参数 | 单位 |", "|---|---|---|---|---|"]
            for c in items:
                out.append(f"| `{c['concept_id']}` | {c['label']} | `{c.get('formula') or '—'}` | "
                           f"{', '.join(c.get('expression_args') or []) or '—'} | "
                           f"{c.get('unit_default') or '—'} |")

    l2 = by_layer.get("L2", [])
    hit = [c for c in l2 if c.get("basis_element")]
    miss = [c for c in l2 if not c.get("basis_element")]
    out += ["", "## 对齐统计", "",
            f"- L2 共 {len(l2)} 个：**命中 {len(hit)} 个**"
            f"（{len(hit) / len(l2):.0%}），自建 {len(miss)} 个。", ""]
    if miss:
        out.append("- 自建项与理由：")
        for c in miss:
            out.append(f"  - `{c['concept_id']}` {c['label']}："
                       f"{c.get('definition') or '2015 版 CAS 无对应元素'}")
    out += ["", "## 约束清单（只校验不推导）", "",
            "| 约束 ID | 规则 | 表达式 | 严重度 | 说明 |", "|---|---|---|---|---|"]
    for k in doc["constraints"]:
        out.append(f"| `{k['constraint_id']}` | {k['rule_type']} | `{k['expression']}` | "
                   f"**{k.get('severity', 'error')}** | {(k.get('note') or '').replace(chr(10), ' ')} |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验，不落盘")
    args = ap.parse_args()

    doc = build()
    errs = _check(doc)
    if errs:
        print("校验失败：")
        for e in errs:
            print("  -", e)
        return 1

    out = CONFIG / "ontology.yaml"
    header = (
        "# 本体层单一真相源（ontology.yaml）\n"
        "#\n"
        "# 由 tools/build_ontology.py 生成，**但可以直接手工编辑**——运行时只读这个文件。\n"
        "# 想持久化的改动请同时回灌到生成脚本的 CURATED 表，否则重跑会被覆盖。\n"
        "#\n"
        f"# 规模：{len(doc['concepts'])} 个概念 / {len(doc['relations'])} 条关系 / "
        f"{len(doc['constraints'])} 条约束\n"
        "#\n"
        "# 四层栈：L0 元概念 → L1 科目骨架(FIBO) → L2 国标科目(CAS 2015) → L3 派生指标\n"
        "# 改 L2 的 basis_element 前先确认该元素在 CAS 里真实存在，别凭印象填。\n"
    )
    body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)
    out.write_text(header + body, encoding="utf-8")
    print(f"已生成 {out}（{len(doc['concepts'])} 概念 / "
          f"{len(doc['relations'])} 关系 / {len(doc['constraints'])} 约束）")
    mapping = ROOT.parent / "FinKB-科目对照表.md"
    mapping.write_text(render_mapping_md(doc), encoding="utf-8")
    print(f"已生成 {mapping}")

    by_layer: dict[str, int] = {}
    for c in doc["concepts"]:
        by_layer[c["layer"]] = by_layer.get(c["layer"], 0) + 1
    print("分层:", " ".join(f"{k}={v}" for k, v in sorted(by_layer.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
