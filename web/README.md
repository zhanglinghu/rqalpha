# RQAlpha Web View

这个目录现在支持两种模式：

- `index.html`：网页界面
- `result.json`：静态示例数据

## 模式 1：静态演示模式

适合只看页面结构和示例数据。

在 `rqalpha` 根目录执行：

```bash
python3 -m http.server 8000
```

然后在浏览器访问：

```text
http://127.0.0.1:8000/web/index.html
```

此时页面会读取：

- `web/result.json`

## 模式 2：网页直接触发本地 RQAlpha

适合在网页里点击按钮，直接运行内置示例策略。

先激活你安装 RQAlpha 时创建的虚拟环境，然后在 `rqalpha` 根目录执行：

```bash
source .venv/bin/activate
rqalpha web --host 127.0.0.1 --port 8000
```

然后浏览器访问：

```text
http://127.0.0.1:8000/web/index.html
```

页面加载后：

1. 会先探测本地 API 是否存在
2. 如果存在，就进入“网页触发本地 RQAlpha”模式
3. 点击“运行内置示例策略”按钮
4. 页面会调用本地 RQAlpha 执行内置 `buy_and_hold.py`
5. 回测完成后自动刷新参数、KPI、收益曲线和交易记录

## 当前第一版限制

第一版固定运行内置最小示例策略：

- `rqalpha/examples/buy_and_hold.py`

也就是说：

- 不能在网页里任意选本地策略文件
- 不能在网页里编辑策略代码
- 还没有参数表单

但网页 → 本地 RQAlpha → 回测结果 → 页面展示 这条链路已经是完整目标方向。

## 建议下一步

如果你后续还想继续扩展，最自然的下一步是：

- 允许在网页里选择策略文件
- 允许修改回测日期 / 初始资金 / benchmark
- 增加运行日志面板
- 增加回撤、超额收益、仓位变化图
