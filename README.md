# MA5 Strategy Lab

个人用的盘后复盘工具。主要用来做 MA5/B 点选股、自选池跟踪、单票回测和批量验证。

## 启动

```powershell
cd D:\Documents\stock_backtester
python web_app.py
```

如果用 Codex 自带 Python：

```powershell
cd D:\Documents\stock_backtester
C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe web_app.py
```

浏览器打开：

```text
http://127.0.0.1:8765/
```

也可以双击 `start_strategy_tester.bat`。

## 常用入口

- `/`：行动台
- `/us/scanner`：美股选股器
- `/us/watchlist`：美股自选池
- `/us/backtest`：美股回测
- `/us/batch`：美股批量回测
- `/cn/scanner`：A股选股器
- `/cn/watchlist`：A股自选池
- `/cn/backtest`：A股回测

## 数据和缓存

- `data/`：自选池、扫描结果、行情缓存等本地数据
- `reports/`：回测和扫描生成的 HTML/CSV 报告

这些目录不要提交到 GitHub。

网页行动台里有缓存维护区，可以直接清理报告、行情缓存、美股缓存、A股缓存和最新扫描结果。清理不会删除自选池。

云端部署时可以用环境变量指定数据目录：

```powershell
$env:MA5_DATA_DIR="D:\ma5_data"
$env:MA5_REPORT_DIR="D:\ma5_reports"
$env:MA5_HOST="0.0.0.0"
$env:MA5_PORT="8765"
python web_app.py
```

## 说明

这个项目只用于个人复盘和策略研究，不构成投资建议。实盘前需要自己核验价格、成交量、财报日期、新闻和交易规则。
