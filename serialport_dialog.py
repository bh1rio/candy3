# -*- coding: utf-8 -*-
"""串口选择对话框。

列出系统中当前可用的串口（基于 pyserial 的 list_ports），
选择后确定返回串口号，取消返回 None。
"""

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QLabel)
from serial.tools import list_ports


class SerialPortDialog(QDialog):
    """选择可用串口的对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择串口")
        self.setMinimumWidth(420)
        self.selected_port = None

        self.combo = QComboBox()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QGridLayout()
        layout.addWidget(QLabel("串口"), 0, 0)
        layout.addWidget(self.combo, 0, 1)
        layout.addWidget(buttons, 1, 0, 1, 2)
        self.setLayout(layout)
        self.layout().setContentsMargins(12, 12, 12, 12)

        self.refresh()

    def refresh(self):
        """重新枚举系统中的串口。"""
        self.combo.clear()
        ports = sorted(list_ports.comports())
        if not ports:
            self.combo.addItem("(未发现可用串口)", None)
            return
        for p in ports:
            desc = (" - %s" % p.description) if p.description else ""
            self.combo.addItem("%s%s" % (p.device, desc), p.device)

    def _on_accept(self):
        port = self.combo.currentData()
        if not port:
            return
        self.selected_port = port
        self.accept()

    def exec_selected(self):
        """模态执行；返回选中的串口号（str），取消或无端口时返回 None。"""
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        return self.selected_port
