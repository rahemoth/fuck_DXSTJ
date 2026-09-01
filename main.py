# -*- coding: utf-8 -*-
"""fuck_DXSTJ 入口:启动 GUI"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gui.main_window import MainWindow


def main():
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("fuck_DXSTJ - 学习通自动做题")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
