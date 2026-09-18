## 截图搜题功能实施计划

### 核心结论(探查所得)
- `QuestionLocator` 不可复用:它要求「N.(单选题)」锚点正则,任意截图不带此格式会返回空。改用 OCR 块按阅读序拼接纯文本 + `LLMClient.chat()` 自定义提示词(要求答案+解析)。`Solver` 的系统提示词明确禁止解释,也不适用。
- 项目无通用屏幕截取能力(WindowCapture 绑死学习通窗口)。用已有依赖 Pillow 的 `ImageGrab.grab(all_screens=True)` 冻结全屏,零新增依赖。
- `OcrEngine.run(PIL Image) -> list[OcrBlock]` 可直接复用,块已按 (y1,x1) 排序;小图预处理沿用 executor 既有先例(3× LANCZOS 放大 + retry_threshold 降阈)。

### 新文件 gui/screenshot.py(~350 行)

**① capture_virtual_screen() -> (PIL Image, QRect 虚拟桌面并集, dpr)**
ImageGrab 全屏物理像素图 + QGuiApplication.screens() 几何并集 + 主屏 devicePixelRatio。

**② RegionSelector(QWidget) — 微信式选区覆盖层**
- 无边框置顶 Tool 窗口,geometry=虚拟桌面;背景画冻结截图(QImage setDevicePixelRatio(dpr) 保证逻辑坐标 1:1)。
- paintEvent:整屏压暗(黑 43%)→ 选区内重绘原图提亮 → 琥珀 2px 边框 + 四角 theme.draw_corner_bracket L 形角标 + 选区上方 W×H 尺寸标签。
- 交互:拖拽框选;选区内拖动平移;四角 8px 热区缩放;ESC/右键取消;Enter 确认;松开后选区右下弹迷你工具条(重选/确认)。
- 信号 confirmed(QRect 逻辑坐标) / cancelled();颜色走 ark(),连 themeChangedFinished 重绘。

**③ ScreenshotSearchWorker(QThread) — 沿用 EnvDetectWorker 模式**
- 信号 stage(str) / result(dict) / error(str)。
- 流程:逻辑选区×dpr 裁剪物理像素 → 小图(高<60 或宽<300)3× LANCZOS 放大+降阈 1111→ OcrEngine.run → 无块报「未识别到文字」→ 阅读序拼接 → LLMClient(cfg["llm"]).chat(SYSTEM, text)。
- 系统提示词强制输出【答案】/【解析】(分步 150-400 字、OCR 噪声容错、信息不足明说);无标记时回退展示原文。

**④ ScreenshotResultDialog(MessageBoxBase) — 与 ConfigDialog 同款视觉**
- SmoothScrollArea:截图缩略图、识别文本(等宽)、答案(琥珀强调)、解析;识别中 IndeterminateProgressBar + 阶段文字。
- yesButton→「关闭」,hideCancelButton 后自加 PushButton(FIF.COPY,"复制全部")(答案+解析入剪贴板)。
- 方法 set_stage/set_result/set_error 由 worker 信号驱动。

### gui/main_window.py(小改 ~40 行)
- 标题栏插入 btn_shot = TransparentToolButton(FIF.CAMERA)(CAMERA 已验证存在,不与 SEARCH 环境检测撞语义),顺序:网课助手→截图搜题→环境检测→设置→主题。
- on_screenshot_search():API Key 校验(同 on_start 的 InfoBar.warning)→ capture → RegionSelector → confirmed 裁剪/关覆盖层/开结果窗/起 worker(引用存 self._shot_worker 防 GC);cancelled 清理。

### 不改
core/ 零改动、线程模型不动、config.yaml 不加键(复用 llm/ocr 节)。

### 验证
1. pytest 68 基线全绿。
2. 无头 OCR 链路:PIL 画中文题目图 → 放大 → OcrEngine.run → 断言识别出关键文字。
3. 离屏渲染:RegionSelector(合成冻结图+程序化设选区,验证压暗/提亮/角标/工具条)、结果窗三态(识别中/成功/失败)。
4. LLM 不自动化实测(省 API 额度),路径与现有 LLMClient.chat 用法一致。

### 取舍
- 多屏混合 DPI 按主屏 dpr 统一换算(主流 Windows 场景成立),代码注释注明。
- 不做全局快捷键(需 RegisterHotKey 原生钩子,超范围,留后续)。
- 不接 QuestionLocator(理由见上)。