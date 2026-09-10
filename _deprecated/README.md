# _deprecated — 废弃源码归档

本目录存放分支合并后不再使用、但保留备查的历史源码。**请勿在此目录内开发**;
其中的代码不参与构建与测试,也不保证能与当前主干协同运行。

## 目录说明

### `透明ui_截长图分支/`

来源:分支 `透明ui+截长图识别`(提交 7350b07)。

该分支在开发「长截图批量做题」时,曾把设置对话框/主窗口改为
「iOS 26 液态玻璃」风格(手写 QSS 渐变 + 玻璃胶囊按钮,基于裸 QDialog)。
合并时按决定 **UI 统一采用 master 的 Fluent(qfluentwidgets)实现**,
这套玻璃风格界面整体废弃,仅留档。

- `config_dialog.py` — 液态玻璃风格设置对话框
- `main_window.py` — 配套主窗口(含长截图模式的界面改动)

### `逐题executor_主分支/`

来源:master(提交 a7cb200)。

master 的 `core/pipeline/executor.py` 是纯「逐题做题」实现,滚动依赖
滚轮(`scroll_clicks`/`scroll_wait` 配置键)。长截图分支用「整页扫描 →
批量作答 + 逐题回退」的新 executor 整体取代了它(新实现同时内置 `_loop`
逐题回退、Home 键回顶、方向键微滚,并移除了上述滚轮配置键)。
合并后保留新实现,旧版整体归档。

### `debug_调试脚本/`

来源:master 根目录的临时调试脚本与截图(`debug_*.py` / `debug_*.png`)。
长截图分支开发时已从根目录删除,归档于此备查。

## 与当前主干的对应关系

| 归档文件 | 被谁取代 |
| --- | --- |
| `透明ui_截长图分支/config_dialog.py` | `gui/config_dialog.py`(Fluent 版) |
| `透明ui_截长图分支/main_window.py` | `gui/main_window.py`(Fluent 版) |
| `逐题executor_主分支/executor.py` | `core/pipeline/executor.py`(批量+逐题回退) |
| `debug_调试脚本/*` | (无对应,纯调试产物) |
