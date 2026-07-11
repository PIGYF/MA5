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
http://127.0.0.1:8765/app/
```

也可以双击 `start_strategy_tester.bat`。

工作台使用 React + Vite，Python 服务提供策略、扫描任务、缓存、自选池 API，以及图表和回测报告端点。日常使用直接打开 `/app/`；旧页面地址会自动跳转到新版对应功能。

修改 `frontend/` 后需要重新构建：

```powershell
cd frontend
pnpm install
pnpm build
```

`main` 分支的 GitHub Actions 会先重新构建前端，并检查 `frontend/dist` 是否与源码一致；验证通过后才会连接阿里云执行部署。提交前必须把最新的 `frontend/dist` 一起提交。

## 常用入口

- `/app/scan`：新版美股选股器
- `/app/watchlist`：新版美股自选池
- `/app/backtest`：新版美股回测
- `/app/batch`：新版美股批量回测
- `/app/cn/scan`：新版A股选股器
- `/app/cn/watchlist`：新版A股自选池
- `/app/cn/backtest`：新版A股回测

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
