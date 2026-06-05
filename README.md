# MA5 Strategy Lab

MA5 Strategy Lab 是一个本地/云端可运行的美股盘后复盘工具，用于 MA5 动能 B 点选股、单票回测、批量回测和自选池跟踪。

当前版本主要支持美股，数据源使用 `yfinance`。后续计划讨论 A 股市场支持，但 A 股策略、数据源和交易规则会独立设计。

## 功能

- 首页复盘面板：查看大盘环境、最新扫描结果、自选池摘要和快捷入口。
- 选股器：按市值、成交量、资产类型等条件扫描美股，筛选下一交易日可执行的 B 点。
- 大盘环境提醒：默认使用 QQQ 判断 `Risk-On / Neutral / Risk-Off`，只做提醒，不强制过滤。
- 财报风险提示：候选股显示财报 Badge，并支持隐藏 3 天内、7 天内或未知财报的股票。
- 单票回测：按信号后下一交易日开盘成交，显示策略、买入持有和纳斯达克指数对比。
- 批量回测：用同一套参数批量验证多个股票。
- 自选池：TradingView 风格的左侧自选列表 + 右侧日 K 图和基本信息面板。
- 缓存与报告清理：行情、财报和扫描结果会缓存在本地 `data/`，报告输出到 `reports/`。

## 安装

建议使用 Python 3.10+。

```powershell
cd D:\Documents\stock_backtester
python -m pip install -r requirements.txt
```

如果使用 Codex 自带 Python：

```powershell
cd D:\Documents\stock_backtester
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install -r requirements.txt
```

## 启动

```powershell
cd D:\Documents\stock_backtester
python web_app.py
```

或使用 Codex 自带 Python：

```powershell
cd D:\Documents\stock_backtester
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe web_app.py
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

也可以双击 `start_strategy_tester.bat` 启动。

## 页面入口

系统按市场分成两套工作台。当前美股功能可用，A 股功能先预留完整入口。

美股：

- `/us`：美股今日复盘面板
- `/us/scanner`：美股下一交易日 B 点选股器
- `/us/scan/latest`：查看当前信号日期保存的美股扫描结果
- `/us/watchlist`：美股自选池
- `/us/backtest`：美股单票回测
- `/us/batch`：美股批量回测

A 股预留：

- `/cn`：A 股复盘面板占位
- `/cn/scanner`：A 股选股器，支持单票验证和盘后批量选股；当前硬条件为趋势通过 + J值冰点，量能分用于二次看图确认
- `/cn/watchlist`：A 股自选池占位
- `/cn/backtest`：A 股回测占位
- `/cn/batch`：A 股批量回测占位

旧路径如 `/scanner`、`/watchlist`、`/backtest` 仍兼容为美股入口。

## 策略口径

当前网页端只保留最新的棘轮趋势版策略。

核心规则：

- B 点信号出现在已完成日 K 上。
- 买入执行在信号后的下一交易日开盘。
- 持仓中连续 B 点只作为持仓过程信号，不重复买入。
- S 点或止损信号出现后，卖出执行在下一交易日开盘。
- 回测不使用盘后价或夜盘价。

默认参数：

- MA 周期：5
- 均量周期：20
- 巨量倍数：1.45
- 跌破均线止损：7.5%
- B 点追踪止损：20%
- 反抽距离：4.5%

## 数据目录

默认目录：

- `data/`：本地缓存、扫描结果、自选池
- `reports/`：回测和扫描生成的 HTML/CSV

这些目录不应该提交到 GitHub。云端部署时建议使用环境变量指定持久化目录：

```powershell
$env:MA5_DATA_DIR="D:\ma5_data"
$env:MA5_REPORT_DIR="D:\ma5_reports"
```

Linux 示例：

```bash
export MA5_DATA_DIR=/var/lib/ma5/data
export MA5_REPORT_DIR=/var/lib/ma5/reports
```

服务地址和端口：

```powershell
$env:MA5_HOST="0.0.0.0"
$env:MA5_PORT="8765"
python web_app.py
```

## GitHub/部署注意事项

- 不要提交 `data/`、`reports/`、缓存文件和本地生成报告。
- 依赖写在 `requirements.txt`。
- 阿里云部署时需要自行处理访问控制，例如工作台密码、反向代理或安全组。
- `yfinance` 可能受网络环境影响，云端建议保证服务器可以稳定访问 Yahoo Finance。

## 当前代码结构

- `web_app.py`：HTTP 服务、页面渲染、扫描任务、自选池页面逻辑。
- `ma5_config.py`：路径、默认参数、日期工具、参数解析和区间校验。
- `ashare_lab.py`：A 股数据与策略模块，当前支持单票日线拉取、股票池过滤、指标计算和候选扫描。
- `backtest.py`：核心回测引擎、策略信号、交易明细、HTML 图表报告。
- `scan_next_b.py`：下一交易日 B 点扫描逻辑和 CLI 报告输出。
- `requirements.txt`：运行依赖。

`web_app.py` 仍然偏大，后续计划继续拆分为 `views.py`、`scanner.py`、`watchlist.py`、`market.py`、`storage.py` 和 `assets.py`。

## 后续计划

- 打分制优化：先做因子验证，再调整技术分和综合权重。
- 消息面打分：重新设计催化剂检查面板，支持人工确认和保存。
- 自选池加入日期：显示加入日期、观察天数，并支持排序。
- A 股支持：已预留 `/cn/...` 市场体系和 `ashare_lab.py` 模块；后续讨论 A 股数据源、独立策略、交易规则和选股池。

## 风险说明

本项目用于个人复盘和策略研究，不构成投资建议。数据可能延迟、缺失或被数据源调整，实盘交易前需要自行核验价格、成交量、财报日期和新闻催化。
