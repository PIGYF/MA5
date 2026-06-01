# 股票本地回测工具

这个脚本复刻你的 TradingView Pine 指标逻辑，并按“信号出现后的下一交易日开盘价”执行交易。

## 1. 准备行情 CSV

CSV 至少需要这些列：

```csv
date,open,high,low,close,volume
2024-01-02,10.00,10.30,9.90,10.20,12345600
2024-01-03,10.25,10.80,10.10,10.70,18880000
```

中文表头也可以，例如：

```csv
日期,开盘,最高,最低,收盘,成交量
```

数据要按时间从旧到新排列。

## 2. 直接拉日线数据回测

也可以直接打开本地网页版本：

```powershell
cd D:\Documents\stock_backtester
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe web_app.py
```

然后访问：

```text
http://127.0.0.1:8765/
```

网页里可以输入股票代码、选择回测周期，并自动对比同期纳斯达克综合指数 `^IXIC`。
也可以双击 `start_strategy_tester.bat` 一键启动。

推荐：

- A 股：`akshare`
- 美股 / 港股：`yfinance`

先安装数据源库，二选一或都装：

```powershell
python -m pip install akshare -U
python -m pip install yfinance -U
```

如果用 Codex 自带 Python：

```powershell
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install akshare -U
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m pip install yfinance -U
```

A 股示例，平安银行 `000001`：

```powershell
cd D:\Documents\stock_backtester
python backtest.py --symbol 000001 --source akshare --start 2020-01-01 --end 2026-05-26
```

美股示例，苹果 `AAPL`：

```powershell
python backtest.py --symbol AAPL --source yfinance --start 2020-01-01 --end 2026-05-26 --report-out AAPL_report.html
```

港股示例，腾讯 `0700.HK`：

```powershell
python backtest.py --symbol 0700.HK --source yfinance --start 2020-01-01 --end 2026-05-26
```

想把拉到的数据顺手保存下来：

```powershell
python backtest.py --symbol 000001 --source akshare --cache-csv 000001_daily.csv
```

## 3. 用本地 CSV 回测

如果电脑安装了 Python：

```powershell
cd D:\Documents\stock_backtester
python backtest.py --csv D:\你的行情数据.csv
```

如果没有安装 Python，可以用 Codex 自带的 Python：

```powershell
cd D:\Documents\stock_backtester
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe backtest.py --csv D:\你的行情数据.csv
```

运行后会输出：

- `trades.csv`：每笔交易明细
- `equity.csv`：每日权益曲线、信号、仓位
- `report.html`：可视化报告，包含价格线、5 日线、买卖点、每笔收益金额柱状图

## 4. 参数

默认参数和你的 TradingView 指标一致：

```powershell
python backtest.py --csv D:\你的行情数据.csv --ma-length 5 --vol-length 50 --vol-multiplier 1.45
```

常用参数：

```powershell
--initial-cash 100000      初始资金
--commission-pct 0.03      单边手续费百分比，0.03 表示 0.03%
--slippage-pct 0.05        单边滑点百分比，0.05 表示 0.05%
--trades-out trades.csv    交易明细输出路径
--equity-out equity.csv    权益曲线输出路径
--report-out report.html   HTML 图表报告输出路径
```

自定义回测区间：

```powershell
python backtest.py --symbol MSFT --source yfinance --start 2021-01-01 --end 2024-12-31 --report-out MSFT_2021_2024.html
```

生成后直接双击 HTML 报告文件，或者在浏览器中打开它即可查看图表。

## 5. 交易规则

- 空仓时，第一次出现买入信号，下一交易日开盘买入。
- 持仓时，第一次出现离场信号，下一交易日开盘卖出。
- 连续买入信号不会重复买入。
- 连续卖出信号不会重复卖出。
- 默认满仓买入，按整股数量计算。
