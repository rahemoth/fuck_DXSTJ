# -*- coding: utf-8 -*-
"""
主窗口:
- 顶部:连接状态 / 连接测试 / 开始 / 停止 / 设置
- 中部:当前题目与答案预览、运行统计
- 底部:滚动日志区

线程模型:GUI 主线程 + Worker(QThread),Executor 事件经信号转发回 GUI。
"""
from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QPlainTextEdit, QGroupBox, QFormLayout, QMessageBox, QApplication,
    QDialog, QTreeWidget, QTreeWidgetItem, QDialogButtonBox, QComboBox,
)

from core.config import Config
from core.log import setup_logging, set_gui_callback, get_logger
from core.pipeline.executor import Executor, ExecutorEvent
from gui.config_dialog import ConfigDialog


class LogBridge(QObject):
    """日志信号桥:工作线程发日志 → 信号(自动排队)→ GUI 主线程追加"""
    log_signal = Signal(str)


class Worker(QThread):
    """承载 Executor 主循环的工作线程(客户端 OCR 版 / 网页版)"""
    event = Signal(str, dict)  # kind, data

    def __init__(self, cfg: dict, web_mode: bool = False):
        super().__init__()
        if web_mode:
            from core.web.driver import WebExecutor
            self.executor = WebExecutor(cfg, emit=self._on_event)
        else:
            self.executor = Executor(cfg, emit=self._on_event)

    def _on_event(self, e: ExecutorEvent):
        self.event.emit(e.kind, e.data)

    def run(self):
        self.executor.run()

    def stop(self):
        self.executor.stop()


class EnvDetectDialog(QDialog):
    """环境检测结果展示:树形列表(IDE / Agent / Python / 工具链)"""

    SECTION_TITLES = {
        "ides": "IDE / 编辑器",
        "agents": "AI 编程 Agent",
        "pythons": "Python",
        "toolchains": "语言工具链",
    }

    def __init__(self, result: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("环境检测")
        self.resize(760, 480)
        layout = QVBoxLayout(self)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["名称", "版本 / 来源", "路径"])
        self.tree.setColumnWidth(0, 200)
        self.tree.setColumnWidth(1, 260)
        for section, items in result.items():
            title = self.SECTION_TITLES.get(section, section)
            group = QTreeWidgetItem([f"{title} ({len(items)})"])
            group.setFlags(Qt.ItemIsEnabled)  # 仅分组标题,不可选
            if not items:
                empty = QTreeWidgetItem(["(未检测到)"])
                empty.setFlags(Qt.NoItemFlags)
                group.addChild(empty)
            for it in items:
                ver = it.get("version") or f"来源: {it['source']}"
                child = QTreeWidgetItem([it["name"], ver, it["path"]])
                group.addChild(child)
            group.setExpanded(True)
            self.tree.addTopLevelItem(group)
        layout.addWidget(self.tree)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.accept)
        layout.addWidget(buttons)


class EnvDetectWorker(QThread):
    """后台线程执行环境检测(含版本探测的 subprocess,不阻塞 GUI)"""
    finished_signal = Signal(dict)

    def run(self):
        from core.env.detector import detect_all
        try:
            self.finished_signal.emit(detect_all())
        except Exception as e:
            self.logger_error = str(e)
            self.finished_signal.emit({})


class WebConnectWorker(QThread):
    """后台线程执行网页版连接测试(CDP 检测,不阻塞 GUI)"""
    result_signal = Signal(bool, str)

    def __init__(self, web_cfg: dict):
        super().__init__()
        self.web_cfg = web_cfg

    def run(self):
        from core.web.driver import test_connection
        try:
            self.result_signal.emit(*test_connection(self.web_cfg))
        except Exception as e:
            self.result_signal.emit(False, f"网页版连接测试异常: {e}")


class ShortcutPortWorker(QThread):
    """后台线程:为浏览器快捷方式一键追加调试端口参数"""
    result_signal = Signal(list)

    def __init__(self, port: int):
        super().__init__()
        self.port = port

    def run(self):
        from core.web.shortcut import add_port_to_all
        try:
            self.result_signal.emit(add_port_to_all(self.port))
        except Exception as e:
            self.result_signal.emit([{"path": "", "ok": False,
                                      "msg": f"快捷方式处理异常: {e}"}])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        setup_logging()
        self.logger = get_logger("gui")

        self.setWindowTitle("fuck_DXSTJ - 学习通自动做题")
        self.resize(860, 640)
        self.worker: Worker | None = None

        self._build_ui()
        # 日志经信号桥跨线程安全转发到 GUI
        self._log_bridge = LogBridge()
        self._log_bridge.log_signal.connect(self.log_view.appendPlainText)
        set_gui_callback(self._log_bridge.log_signal.emit)

    # ---------- UI 构建 ----------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 顶部控制栏
        top = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("客户端(OCR)")
        self.mode_combo.addItem("网页版(浏览器)")
        self.mode_combo.setToolTip(
            "客户端:OCR 识别学习通 PC 客户端\n"
            "网页版:CDP 直连浏览器 DOM 读题(需浏览器开调试端口,自动拉起)")
        top.addWidget(self.mode_combo)
        self.status_label = QLabel("● 未连接")
        self.status_label.setStyleSheet("color: gray; font-weight: bold;")
        top.addWidget(self.status_label)
        top.addStretch()

        self.btn_connect = QPushButton("测试连接")
        self.btn_connect.clicked.connect(self.on_connect)
        top.addWidget(self.btn_connect)

        self.btn_start = QPushButton("开始")
        self.btn_start.setStyleSheet("font-weight: bold;")
        self.btn_start.clicked.connect(self.on_start)
        top.addWidget(self.btn_start)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)
        top.addWidget(self.btn_stop)

        self.btn_config = QPushButton("设置")
        self.btn_config.clicked.connect(self.on_config)
        top.addWidget(self.btn_config)

        self.btn_env = QPushButton("环境检测")
        self.btn_env.clicked.connect(self.on_env_detect)
        top.addWidget(self.btn_env)

        self.btn_port = QPushButton("一键开启调试端口")
        self.btn_port.setToolTip(
            "为桌面/开始菜单/任务栏的 Edge/Chrome 快捷方式追加 --remote-debugging-port 参数。\n"
            "之后每次正常打开浏览器都自带调试端口,程序无需重启浏览器即可直连。\n"
            "改完后需完全退出并重新打开浏览器才生效。")
        self.btn_port.clicked.connect(self.on_add_port)
        top.addWidget(self.btn_port)
        root.addLayout(top)

        # 中部:题目预览 + 统计
        preview_box = QGroupBox("当前题目 / 答案预览")
        preview_form = QFormLayout(preview_box)
        self.lbl_type = QLabel("-")
        self.lbl_stem = QLabel("-")
        self.lbl_stem.setWordWrap(True)
        self.lbl_options = QLabel("-")
        self.lbl_options.setWordWrap(True)
        self.lbl_answer = QLabel("-")
        self.lbl_answer.setStyleSheet("font-weight: bold; color: #0a6;")
        preview_form.addRow("题型:", self.lbl_type)
        preview_form.addRow("题干:", self.lbl_stem)
        preview_form.addRow("选项:", self.lbl_options)
        preview_form.addRow("模型答案:", self.lbl_answer)
        root.addWidget(preview_box)

        # 统计行
        stat = QHBoxLayout()
        self.lbl_stat = QLabel("完成 0 题 / 失败 0 题")
        stat.addWidget(self.lbl_stat)
        stat.addStretch()
        root.addLayout(stat)

        # 底部:日志
        log_box = QGroupBox("日志")
        log_layout = QVBoxLayout(log_box)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log_view)
        root.addWidget(log_box, stretch=1)

    # ---------- 按钮事件 ----------

    def on_connect(self):
        if self.mode_combo.currentIndex() == 1:
            self.on_connect_web()
            return
        from core.controller.window import WindowCapture

        cfg = Config.get()
        win = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
        if win.find():
            try:
                img = win.screenshot()
                self.status_label.setText("● 已连接")
                self.status_label.setStyleSheet("color: green; font-weight: bold;")
                self.logger.info(f"连接成功,截图尺寸 {img.size}")
            except Exception as e:
                self.status_label.setText("● 窗口已找到,但截图失败")
                self.status_label.setStyleSheet("color: orange; font-weight: bold;")
                self.logger.error(f"截图失败: {e}")
        else:
            self.status_label.setText("● 未找到学习通窗口")
            self.status_label.setStyleSheet("color: red; font-weight: bold;")
            self.logger.warning("未找到学习通窗口,请打开学习通PC客户端后重试")

    def on_config(self):
        cfg = Config.get()
        dlg = ConfigDialog(cfg.data, self)
        if dlg.exec():
            updates = dlg.collect()
            for section, values in updates.items():
                cfg.update(section, values)
            cfg.reload()
            self.logger.info("配置已保存")
            if cfg["action"]["dry_run"]:
                self.logger.info("当前为 dry-run 模式:只识别和请求答案,不会点击")

    def on_connect_web(self):
        """网页版测试连接:后台线程检测 CDP 端口与学习通页面"""
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("测试中...")
        self.logger.info("网页版:测试浏览器调试端口连接 ...")
        self._web_worker = WebConnectWorker(Config.get()["web"])
        self._web_worker.result_signal.connect(self._on_web_connect_result)
        self._web_worker.start()

    def _on_web_connect_result(self, ok: bool, msg: str):
        self.btn_connect.setEnabled(True)
        self.btn_connect.setText("测试连接")
        if ok:
            self.status_label.setText("● 已连接(网页版)")
            self.status_label.setStyleSheet("color: green; font-weight: bold;")
            self.logger.info(f"网页版连接测试通过: {msg}")
        else:
            self.status_label.setText("● 网页版未就绪")
            self.status_label.setStyleSheet("color: orange; font-weight: bold;")
            self.logger.warning(f"网页版连接测试失败: {msg}")

    def on_add_port(self):
        """一键为浏览器快捷方式追加调试端口参数"""
        port = Config.get()["web"].get("cdp_port", 9222)
        ret = QMessageBox.question(
            self, "一键开启调试端口",
            f"将为桌面/开始菜单/任务栏中的 Edge/Chrome 快捷方式追加\n"
            f"--remote-debugging-port={port} 参数。\n\n"
            f"之后每次正常打开浏览器都自带调试端口,\n"
            f"程序可直接连接,无需重启浏览器。\n\n"
            f"注意:开启调试端口期间本机程序均可控制该浏览器,\n"
            f"敏感操作(网银等)建议使用未开启端口的浏览器。\n\n"
            f"是否继续?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if ret != QMessageBox.Yes:
            return
        self.btn_port.setEnabled(False)
        self.btn_port.setText("处理中...")
        self._sc_worker = ShortcutPortWorker(port)
        self._sc_worker.result_signal.connect(self._on_port_added)
        self._sc_worker.start()

    def _on_port_added(self, results: list):
        self.btn_port.setEnabled(True)
        self.btn_port.setText("一键开启调试端口")
        ok = sum(1 for r in results if r["ok"])
        lines = [f"{('√' if r['ok'] else '×')} {r['msg']}\n   {r['path']}" for r in results]
        QMessageBox.information(
            self, "调试端口",
            f"处理完成:{ok}/{len(results)} 个快捷方式成功。\n\n" + "\n".join(lines) +
            "\n\n请完全退出浏览器后重新打开(开始菜单/桌面/任务栏图标),即可生效。")
        self.logger.info(f"快捷方式调试端口处理完成: {ok}/{len(results)} 成功")

    # ---------- 环境检测 ----------

    def on_env_detect(self):
        """后台线程检测本机 IDE/Agent/Python/工具链,结果弹窗展示"""
        self.btn_env.setEnabled(False)
        self.btn_env.setText("检测中...")
        self.logger.info("开始检测本机开发环境...")
        self._env_worker = EnvDetectWorker()
        self._env_worker.finished_signal.connect(self._on_env_detected)
        self._env_worker.start()

    def _on_env_detected(self, result: dict):
        self.btn_env.setEnabled(True)
        self.btn_env.setText("环境检测")
        if not result:
            self.logger.error(f"环境检测失败: {getattr(self._env_worker, 'logger_error', '未知错误')}")
            return
        dlg = EnvDetectDialog(result, self)
        dlg.exec()

    def on_start(self):
        cfg = Config.get()
        if not cfg["llm"]["api_key"] or "xxxx" in cfg["llm"]["api_key"]:
            QMessageBox.warning(self, "缺少配置",
                                "请先在【设置】中填写 API Key 和模型信息")
            return
        if cfg["action"]["dry_run"]:
            self.logger.info("dry-run 模式已开启:不会实际点击")

        web_mode = self.mode_combo.currentIndex() == 1
        if not web_mode:
            # 客户端模式:重连窗口
            from core.controller.window import WindowCapture
            win = WindowCapture(cfg["window"]["title_keywords"], cfg["window"]["capture_method"])
            if not win.find():
                QMessageBox.warning(self, "未找到窗口", "未找到学习通窗口,请先打开学习通PC客户端")
                return
        else:
            self.logger.info("网页版模式:将连接调试端口的浏览器(未启动会自动拉起 Chrome/Edge)")

        self.worker = Worker(cfg.data, web_mode=web_mode)
        self.worker.event.connect(self.on_worker_event)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_connect.setEnabled(False)

    def on_stop(self):
        if self.worker:
            self.worker.stop()

    def on_worker_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_connect.setEnabled(True)

    def on_worker_event(self, kind: str, data: dict):
        if kind == "question":
            type_names = {"single": "单选题", "multiple": "多选题", "judge": "判断题"}
            self.lbl_type.setText(type_names.get(data["qtype"], data["qtype"]))
            self.lbl_stem.setText(data["stem"] or "(空)")
            opts = data.get("options") or {}
            self.lbl_options.setText("\n".join(f"{k}. {v}" for k, v in opts.items()) or "-")
            self.lbl_answer.setText("(等待模型...)")
        elif kind == "answer":
            self.lbl_answer.setText(" / ".join(data["answer"]))
        elif kind == "done":
            self.lbl_stat.setText(data.get("summary", ""))
            self.lbl_answer.setText("-")
        elif kind == "error":
            QMessageBox.critical(self, "运行错误", data.get("message", "未知错误"))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        event.accept()
