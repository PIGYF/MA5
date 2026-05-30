# web_site

这是为 `PIGYF-MA5` 新增的独立 Web 登录壳。

## 目标

- 不改原有策略逻辑
- 不改原有页面语义
- 登录成功后继续使用原 `web_app.py` 生成的页面
- 作为 `ma5.drypeek.top` 的前置登录层部署

## 结构

- `app.py`：HTTP 入口、登录处理、会话校验、代理转发
- `auth.py`：密码校验与内存会话
- `proxy.py`：转发到原 `web_app.py`
- `constants.py`：端口、路径、Cookie、密码等常量
- `static/login.html`：登录页模板

## 本地运行

1. 先启动原有 `web_app.py`，保持它监听 `127.0.0.1:8765`
2. 再运行：

```text
python web_site/app.py
```

3. 打开：

```text
http://127.0.0.1:8764/login
```

## 当前默认值

- 登录密码：`admin`
- 会话有效期：24 小时
- `web_site` 监听：`127.0.0.1:8764`
- `web_app.py` 上游：`127.0.0.1:8765`

## 部署建议

生产环境推荐使用：

- `ma5.drypeek.top` -> 现有反向代理 / 站点栈
- 站点栈 -> `web_site`
- `web_site` -> 本机 `web_app.py`

即：公网不直接访问 `web_app.py`。
