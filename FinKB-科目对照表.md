# FinKB 本体科目对照表（L0–L3 全量）

> 由 `finkb/tools/build_ontology.py` 从 `config/ontology.yaml` 与 CAS 2015 标签表（4317 个元素）生成，不是手写。改 YAML 后重跑该脚本即可刷新。

> 规模：62 概念 / 71 关系 / 10 约束；CAS 元素总数 4317（cas_* 3682 + ifrs-full_* 635）。


## L0 元概念（跨域通用，十年不改）（7 个）

| 概念 ID | 中文名 | 父概念 | basis | 标准定位 | 说明 |
|---|---|---|---|---|---|
| `cmns:MonetaryAmount` | 金额 | — | cmns | https://www.omg.org/spec/Commons/QuantitiesAndUnits/MonetaryAmount | 以货币表示的数额，必须同时绑定币种与期间（存量指标用时点）。 |
| `xbrli:perShareItem` | 每股金额 | — | ifrs | http://www.xbrl.org/2003/instance/perShareItemType | 按股数摊薄后的货币金额，单位固定为「元/股」。 |
| `xbrli:pureItem` | 比率 | — | ifrs | http://www.xbrl.org/2003/instance/pureItemType | 无量纲比值，本项目统一以百分数存储（毛利率 17.74 表示 17.74%）。 |
| `cmns-qtu:Expression` | 派生表达式 | — | cmns | https://www.omg.org/spec/Commons/QuantitiesAndUnits/Expression | 由其他概念经算术运算得到的值；必须声明参数（expression_args）才能计算。 |
| `cmns-dt:DatePeriod` | 期间 | — | cmns | https://www.omg.org/spec/Commons/DatesAndTimes/DatePeriod | 有起止日的区间。利润表/现金流量表类概念必须有它，资产负债表类必须没有（时点）。 |
| `cmns-qtu:Measure` | 计量单位 | — | cmns | https://www.omg.org/spec/Commons/QuantitiesAndUnits/Measure | 数值的附属单位，本项目限定为 亿元 / % / 元每 / 辆。 |
| `cmns:Party` | 主体 | — | cmns | https://www.omg.org/spec/Commons/PartiesAndSituations/Party | 财报的报告主体（上市公司）。所有事实都必须挂在一个主体上。 |

## L1 科目骨架（借 FIBO 语义）（13 个）

| 概念 ID | 中文名 | 父概念 | basis | 标准定位 | 说明 |
|---|---|---|---|---|---|
| `fibo:Income` | 收入 | cmns:MonetaryAmount | fibo | https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/Income | 企业在期间内取得的经济利益流入。FIBO 定义：revenue received during a period of time；约束 hasDatePeriod 基数恰为 1 —— 即收入**必须带期间**，这一条是本层最重要的语义约束。 |
| `finkb:Expense` | 费用 | cmns:MonetaryAmount | self | — | 期间内流出的经济利益（营业成本与三费）。FIBO 闭包内未提供可复用的 Expense 类，按 CAS 费用类科目语义新增，故 basis=self。 |
| `fibo:Asset` | 资产 | cmns:MonetaryAmount | fibo | https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/Asset | 主体在某一时点控制的资源。FIBO 把它分为 流动/非流动/持有待售 三类。 |
| `finkb:Liabilities` | 负债 | cmns:MonetaryAmount | self | — | 主体在某一时点承担的现时义务。FIBO 闭包内无独立 Liabilities 类（负债在 FIBO 里作为金融工具/合同的义务面建模），此处按 CAS 负债类语义新增。 |
| `fibo:OwnersEquity` | 所有者权益 | cmns-qtu:Expression | fibo | https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/OwnersEquity | 资产减负债后的剩余权益。FIBO 把它建模为 Expression（差额表达式）而非直接金额，这正是「权益是算出来的、不是数出来的」的形式化表达。 |
| `fibo:CashFlow` | 现金流 | cmns:MonetaryAmount | fibo | https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/CashFlow | 期间内现金及现金等价物的净流入/流出量。 |
| `fibo:EBITDA` | 息税折旧摊销前利润 | cmns-qtu:Expression | fibo | https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/CurrencyAmount/EBITDA | FIBO 用 hasArgument 声明其由哪些科目合成。本项目 MVP 不用，保留作 L1 扩展位。 |
| `fibo:Report` | 定期报告 | cmns:Party | fibo | https://spec.edmcouncil.org/fibo/ontology/FND/Accounting/Accounting/Report | 主体发布的财务报告文档（年报/半年报/季报）。L0 凭证层的 PDF 即其实例。 |
| `finkb:Dimension` | 维度 | fibo:Report | self | — | 报表披露的拆分视角（分产品/分行业/分地区…）。FIBO 闭包内无对应类，按 CAS 披露规则新增。维度种类封闭，成员开放——这是与「新科目」的分界线。 |
| `dim:region` | 分地区 | finkb:Dimension | cas | http://xbrl.mof.gov.cn/taxonomy/2015-03-31/cas#cas_RegionsAxis | 按地理区域拆分收入/成本。国标对应 cas_RegionsAxis（地区分布 [axis]）。 |
| `dim:product` | 分产品 | finkb:Dimension | self | — | 按产品线拆分。国标无「分产品」axis（实务走「主营业务分产品情况」表），故 basis=self。 |
| `dim:industry` | 分行业 | finkb:Dimension | cas | http://xbrl.mof.gov.cn/taxonomy/2015-03-31/cas#cas_ReportableSegmentsProfitLossAssetAndLiabilityAxis | 按行业分部拆分。国标用报告分部 axis 表达，与「分行业」披露表同构。 |
| `dim:customer` | 分客户 | finkb:Dimension | cas | http://xbrl.mof.gov.cn/taxonomy/2015-03-31/cas#cas_ClassesOfCustomersAxis | 按客户类别拆分（如银行披露的零售/对公）。国标对应客户类别 axis。本项目暂未抽取，先登记为扩展位。 |

## L2 国标科目（对齐 CAS 2015）（36 个）

| # | 概念 ID | 中文名 | metric_id | FIBO 骨架 | CAS 元素 | CAS 中文标签（角色） | 状态 |
|---|---|---|---|---|---|---|---|
| 1 | `metric:revenue` | 营业总收入 | revenue | fibo:Income | `cas_Revenues` | 营业收入（label） | ✅ 命中 |
| 2 | `metric:net_profit` | 归属于上市公司股东的净利润 | net_profit | fibo:Income | `ifrs-full_ProfitLossAttributableToOwnersOfParent` | 归属于母公司所有者（股东）的净利润（label） | ✅ 命中（IFRS 元素） |
| 3 | `metric:net_profit_deducted` | 归属于上市公司股东的扣除非经常性损益的净利润 | net_profit_deducted | fibo:Income | — | — | ⚪ 自建（2015 版无此科目） |
| 4 | `metric:operating_profit` | 营业利润 | operating_profit | fibo:Income | `cas_OperatingProfits` | 营业利润（label） | ✅ 命中 |
| 5 | `metric:total_profit` | 利润总额 | total_profit | fibo:Income | `cas_ProfitLossBeforeIncomeTaxes` | 利润总额（label） | ✅ 命中 |
| 6 | `metric:net_interest_income` | 利息净收入 | net_interest_income | fibo:Income | `cas_NetInterestIncome` | 利息净收入，金融企业（label） | ✅ 命中 |
| 7 | `metric:fee_commission_income` | 手续费及佣金净收入 | fee_commission_income | fibo:Income | `cas_FeeAndCommissionIncome` | 手续费及佣金净收入，金融企业（label） | ✅ 命中 |
| 8 | `metric:insurance_revenue` | 保险服务收入 | insurance_revenue | fibo:Income | `cas_IncomeFromInsuranceBusiness` | 保险业务收入（label） | ✅ 命中 |
| 9 | `metric:operating_cost` | 营业成本 | operating_cost | finkb:Expense | `cas_CostOfRevenues` | 营业成本（label） | ✅ 命中 |
| 10 | `metric:selling_expense` | 销售费用 | selling_expense | finkb:Expense | `cas_SellingAndMarketingExpense` | 销售费用（label） | ✅ 命中 |
| 11 | `metric:admin_expense` | 管理费用 | admin_expense | finkb:Expense | `cas_GeneralAndAdministrativeExpenses` | 管理费用（label） | ✅ 命中 |
| 12 | `metric:finance_expense` | 财务费用 | finance_expense | finkb:Expense | `cas_FinanceExpense` | 财务费用（label） | ✅ 命中 |
| 13 | `metric:rd_expense` | 研发投入 | rd_expense | finkb:Expense | `cas_ResearchAndDevelopmentExpenses` | 研究开发支出（label） | ✅ 命中 |
| 14 | `metric:total_assets` | 资产总额 | total_assets | fibo:Asset | `ifrs-full_Assets` | 资产（label） | ✅ 命中（IFRS 元素） |
| 15 | `metric:current_assets` | 流动资产合计 | current_assets | fibo:Asset | `ifrs-full_CurrentAssets` | 流动资产（label） | ✅ 命中（IFRS 元素） |
| 16 | `metric:cash` | 货币资金 | cash | fibo:Asset | `cas_BankBalancesAndCash` | 货币资金（label） | ✅ 命中 |
| 17 | `metric:inventory` | 存货 | inventory | fibo:Asset | `ifrs-full_Inventories` | 存货（label） | ✅ 命中（IFRS 元素） |
| 18 | `metric:accounts_receivable` | 应收账款 | accounts_receivable | fibo:Asset | `cas_AccountsReceivable` | 应收账款（label） | ✅ 命中 |
| 19 | `metric:loans_advances` | 发放贷款和垫款 | loans_advances | fibo:Asset | `cas_LoansAndAdvances` | 贷款和垫款账面价值（label） | ✅ 命中 |
| 20 | `metric:total_liabilities` | 负债总额 | total_liabilities | finkb:Liabilities | `ifrs-full_Liabilities` | 负债（label） | ✅ 命中（IFRS 元素） |
| 21 | `metric:current_liabilities` | 流动负债合计 | current_liabilities | finkb:Liabilities | `ifrs-full_CurrentLiabilities` | 流动负债（label） | ✅ 命中（IFRS 元素） |
| 22 | `metric:short_term_borrowings` | 短期借款 | short_term_borrowings | finkb:Liabilities | `cas_ShortTermBorrowings` | 短期借款（label） | ✅ 命中 |
| 23 | `metric:long_term_borrowings` | 长期借款 | long_term_borrowings | finkb:Liabilities | `cas_NonCurrentPortionOfLongTermBorrowings` | 长期借款，非流动部分（label） | ✅ 命中 |
| 24 | `metric:bonds_payable` | 应付债券 | bonds_payable | finkb:Liabilities | `cas_BondsPayable` | 应付债券（label） | ✅ 命中 |
| 25 | `metric:lease_liabilities` | 租赁负债 | lease_liabilities | finkb:Liabilities | — | — | ⚪ 自建（2015 版无此科目） |
| 26 | `metric:customer_deposits` | 吸收存款 | customer_deposits | finkb:Liabilities | `cas_CustomerDeposits` | 吸收存款（label） | ✅ 命中 |
| 27 | `metric:total_equity` | 股东权益合计 | total_equity | fibo:OwnersEquity | `ifrs-full_Equity` | 所有者权益（或股东权益）（label） | ✅ 命中（IFRS 元素） |
| 28 | `metric:net_assets` | 归属于上市公司股东的净资产 | net_assets | fibo:OwnersEquity | `ifrs-full_EquityAttributableToOwnersOfParent` | 归属于母公司所有者权益（label） | ✅ 命中（IFRS 元素） |
| 29 | `metric:minority_interest` | 少数股东权益 | minority_interest | fibo:OwnersEquity | `ifrs-full_NoncontrollingInterests` | 少数股东权益（label） | ✅ 命中（IFRS 元素） |
| 30 | `metric:ocf` | 经营活动产生的现金流量净额 | ocf | fibo:CashFlow | `ifrs-full_CashFlowsFromUsedInOperatingActivities` | 经营活动产生的现金流量（label） | ✅ 命中（IFRS 元素） |
| 31 | `metric:icf` | 投资活动产生的现金流量净额 | icf | fibo:CashFlow | `ifrs-full_CashFlowsFromUsedInInvestingActivities` | 投资活动产生的现金流量（label） | ✅ 命中（IFRS 元素） |
| 32 | `metric:fcf` | 筹资活动产生的现金流量净额 | fcf | fibo:CashFlow | `ifrs-full_CashFlowsFromUsedInFinancingActivities` | 筹资活动产生的现金流量（label） | ✅ 命中（IFRS 元素） |
| 33 | `metric:net_cash_increase` | 现金及现金等价物净增加额 | net_cash_increase | fibo:CashFlow | `ifrs-full_IncreaseDecreaseInCashAndCashEquivalents` | 现金及现金等价物净增加额（label） | ✅ 命中（IFRS 元素） |
| 34 | `metric:fx_effect` | 汇率变动对现金及现金等价物的影响 | fx_effect | fibo:CashFlow | `ifrs-full_EffectOfExchangeRateChangesOnCashAndCashEquivalents` | 汇率变动对现金及现金等价物的影响（label） | ✅ 命中（IFRS 元素） |
| 35 | `metric:eps_basic` | 基本每股收益 | eps_basic | xbrli:perShareItem | `ifrs-full_BasicEarningsLossPerShare` | 基本每股收益（label） | ✅ 命中（IFRS 元素） |
| 36 | `metric:roe_weighted` | 加权平均净资产收益率 | roe_weighted | xbrli:pureItem | — | — | ⚪ 自建（2015 版无此科目） |

## L3 派生指标（本项目自建）（6 个）

| 概念 ID | 中文名 | 公式 | 参数 | 单位 |
|---|---|---|---|---|
| `self:gross_margin` | 毛利率 | `(revenue - operating_cost) / revenue` | revenue, operating_cost | % |
| `self:net_margin` | 净利率 | `net_profit / revenue` | net_profit, revenue | % |
| `self:debt_ratio` | 资产负债率 | `total_liabilities / total_assets` | total_liabilities, total_assets | % |
| `self:rd_intensity` | 研发强度 | `rd_expense / revenue` | rd_expense, revenue | % |
| `self:expense_ratio` | 期间费用率 | `(selling_expense + admin_expense + finance_expense) / revenue` | selling_expense, admin_expense, finance_expense, revenue | % |
| `self:equity_ratio` | 权益乘数 | `total_assets / total_equity` | total_assets, total_equity | % |

## 对齐统计

- L2 共 36 个：**命中 33 个**（92%），自建 3 个。

- 自建项与理由：
  - `metric:net_profit_deducted` 归属于上市公司股东的扣除非经常性损益的净利润：「扣除非经常性损益后的净利润」是证监会报表格式行项目，不在 CAS 分类标准内 (basis=self)
  - `metric:lease_liabilities` 租赁负债：「租赁负债」为 CAS 21(2018) 新增，2015 分类标准没有该元素 (basis=self)
  - `metric:roe_weighted` 加权平均净资产收益率：「加权平均净资产收益率」是公司自行披露的监管口径指标，分类标准内无对应元素 (basis=self)

## 约束清单（只校验不推导）

| 约束 ID | 规则 | 表达式 | 严重度 | 说明 |
|---|---|---|---|---|
| `acct.assets_eq_liabilities_plus_equity` | acct_equation | `total_assets == total_liabilities + total_equity` | **error** | 会计恒等式。实测 73 个 (公司,期间) 全部通过——这是本体层最硬的一道体检。 |
| `acct.current_le_total` | range | `current_assets <= total_assets` | **error** | 流动资产合计不可能超过资产总计。 |
| `acct.current_liab_le_total` | range | `current_liabilities <= total_liabilities` | **error** | 流动负债合计不可能超过负债合计。 |
| `acct.cashflow_sum` | acct_equation | `net_cash_increase == ocf + icf + fcf + fx_effect` | **error** | 四项现金流恒等式（v3 修正）：净增加额 = 经营 + 投资 + 筹资 + 汇率变动影响。此前漏掉 fx_effect，导致 8 条合法业务被误报成 warn；补项后升为 error。 |
| `acct.equity_split` | acct_equation | `net_assets + minority_interest == total_equity` | **error** | 权益拆分恒等式（v3 修正）：归母净资产 + 少数股东权益 = 所有者权益合计。旧约束「归母 ≤ 合计」在少数股东权益为负时必然误报（赛力斯 2024/2025Q1）。 |
| `acct.expense_positive` | range | `operating_cost >= 0` | **error** | 费用类科目入库统一取正数：报表里「减：营业成本」常印成括号负数，入口已按 _POSITIVE_METRICS 取绝对值，因此出现负数必是实现 bug。（v3 用它替换了被证伪的 cost_le_revenue，顺带覆盖 finkb:Expense 分支的约束） |
| `range.roe` | range | `-100 <= roe_weighted <= 200` | **warn** | 加权 ROE 的物理边界（百分数口径）；亏损年度或净资产极薄的年份会出界，只报警告。 |
| `range.gross_margin` | range | `-50 <= gross_margin <= 100` | **warn** | 毛利率的物理边界（百分数口径：17.74 = 17.74%）。下界从 -100 收到 -50：毛利率低于 -50% 意味着亏本卖货超过一半，业务上极罕见，更可能是取数错误。 |
| `period.income_requires_period` | required_period | `period IS NOT NULL AND period <> ''` | **error** | FIBO Income 的 hasDatePeriod 基数为 1，落到库里就是期间必填。 |
| `unit.eps_is_per_share` | unit_check | `norm_unit == '元/股'` | **error** | 每股指标的规范化单位必须是元/股。注意查的是 norm_unit 而不是 unit_of_account：后者是原始表格的表头单位标注，每股指标的表头常写成「元」。 |
