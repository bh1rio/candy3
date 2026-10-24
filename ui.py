# -*- coding: utf-8 -*-
"""主窗口：菜单/工具栏/进度条/频道表格/日志框，及“从电台读取/写入/打开/保存镜像”完整流程。"""

import os
import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QDialog, QFileDialog,
                               QHeaderView, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from channel_edit_dialog import ChannelEditDialog
from serialport_dialog import SerialPortDialog
from uv5rmini_radio import (TX_OFF, CHANNEL_COUNT, CHANNEL_IMAGE_SIZE, MEM_TOTAL,
                           RadioError, UV5RMiniRadio, channel_image,
                           decode_tone, is_tx_off)
from uv5rmini_channels import UV5RMiniChannels, reset_channel_empty

ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

# 频道列表表格列定义（覆盖 UV5RMiniChannel 全部有意义字段；
# unknown1/2/7/8、unknown3-6 为未定义保留位，不展示）
CHANNEL_COLUMNS = ["编号", "接收频率", "接收哑音",
                   "发射频率", "发射哑音", "功率", "带宽", "DTMF解码",
                   "信令码", "PTT ID", "遇忙锁定", "扫描", "跳频", "名称"]

# 枚举字段显示名（索引 = 原始值）
PTTID_NAMES = ["无", "按下PPT", "松开PPT", "按下和松开PPT"]        # Chirp LIST_PTTID
SCRAMBLE_NAMES = ["", "SCR1", "SCR2", "SCR3"]   # Chirp SCRAMBLE_LIST
S_CODE_RANGE = (1, 20)                          # S-CODE 显示值 1–20（无“无”）；存储字节 = 显示值 - 1


def icon(name):
    return QIcon(os.path.join(ICONS_DIR, name))


def _freq_text(stored):
    """存储值(=Hz/10) → MHz 文本，如 14500000 -> "145.0000"。"""
    return "%.4f" % (stored * 10 / 1e6)


def _is_empty(ch):
    """空频道：收发频率均全为 0xFF（Chirp 以首字节 0xFF 判定）。"""
    return ch.rxfreq == TX_OFF and ch.txfreq == TX_OFF


def _name_text(raw):
    """12 字节名称 → 文本（剥离 0x00/0xFF 填充，按 GB2312 解码）。"""
    data = bytes(raw).rstrip(b"\x00").rstrip(b"\xff")
    if not data:
        return ""
    try:
        return data.decode("gb2312")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


class ReadWorker(QThread):
    """后台线程：从电台下载镜像。"""

    progress = Signal(int, int)
    log_message = Signal(str)
    finished_ok = Signal(bytes)
    failed = Signal(str)

    def __init__(self, port, parent=None):
        super().__init__(parent)
        self.port = port

    def run(self):
        def log(msg):
            self.log_message.emit(msg)
        try:
            log("打开串口 %s（115200 bps，无流控，超时 1.5 s）…" % self.port)
            radio = UV5RMiniRadio(self.port)
            try:
                image = radio.download(
                    progress_cb=lambda cur, total: self.progress.emit(cur, total),
                    log_cb=log)
            finally:
                radio.close()
                log("串口已关闭")
            log("串口读取完成，频道区共 %d 字节" % len(image))
            self.finished_ok.emit(image)
        except RadioError as exc:
            log("通信错误：%s" % exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            log("读取失败：%s" % exc)
            self.failed.emit("读取电台失败: %s" % exc)


class WriteWorker(QThread):
    """后台线程：把 31968 字节频道区数据写入电台。"""

    progress = Signal(int, int)
    log_message = Signal(str)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(self, port, image, parent=None):
        super().__init__(parent)
        self.port = port
        self.image = image

    def run(self):
        def log(msg):
            self.log_message.emit(msg)
        try:
            log("打开串口 %s（115200 bps，无流控，超时 1.5 s）…" % self.port)
            radio = UV5RMiniRadio(self.port)
            try:
                written = radio.upload(
                    self.image,
                    progress_cb=lambda cur, total: self.progress.emit(cur, total),
                    log_cb=log)
            finally:
                radio.close()
                log("串口已关闭")
            log("串口写入完成，共 %d 字节" % written)
            self.finished_ok.emit(written)
        except RadioError as exc:
            log("通信错误：%s" % exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            log("写入失败：%s" % exc)
            self.failed.emit("写入电台失败: %s" % exc)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.channels = UV5RMiniChannels()
        self._full_image = None      # 最近一次从电台读取的完整镜像（33344 字节）
        self._worker = None

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self._create_table()
        self._create_progressbar()
        self._create_logbox()
        self._create_statusbar()
        self.setWindowTitle("宝锋 UV-5R Mini写频")
        self.setWindowState(Qt.WindowState.WindowMaximized)

    # ---------------- 动作（菜单与工具栏共享） ----------------
    def _create_actions(self):
        self.act_open_image = QAction(icon("folder.png"), "打开镜像", self)
        self.act_save_image = QAction(icon("diskette.png"), "保存镜像", self)
        self.act_read_radio = QAction(icon("inbox_download.png"), "从电台读取", self)
        self.act_write_radio = QAction(icon("inbox_upload.png"), "写入到电台", self)
        self.act_quit = QAction(icon("door_out.png"), "退出", self)
        self.act_quit.triggered.connect(QApplication.quit)
        self.act_about = QAction("关于", self)

        self.act_read_radio.triggered.connect(self._on_read_from_radio)
        self.act_write_radio.triggered.connect(self._on_write_to_radio)
        self.act_save_image.triggered.connect(self._on_save_image)
        self.act_open_image.triggered.connect(self._on_open_image)
        self.act_about.triggered.connect(self._on_about)

    def _create_menus(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件")
        file_menu.addAction(self.act_open_image)
        file_menu.addAction(self.act_save_image)
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)

        op_menu = menubar.addMenu("操作")
        op_menu.addAction(self.act_read_radio)
        op_menu.addAction(self.act_write_radio)

        help_menu = menubar.addMenu("帮助")
        help_menu.addAction(self.act_about)

    def _create_toolbar(self):
        toolbar = self.addToolBar("工具栏")
        toolbar.addAction(self.act_open_image)
        toolbar.addAction(self.act_save_image)
        toolbar.addSeparator()
        toolbar.addAction(self.act_read_radio)
        toolbar.addAction(self.act_write_radio)

    def _create_table(self):
        """创建频道列表数据表格（独立控件，不放在 tab 容器内）。"""
        table = QTableWidget(0, len(CHANNEL_COLUMNS))
        table.setHorizontalHeaderLabels(CHANNEL_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._on_channel_menu)
        table.doubleClicked.connect(self._on_channel_doubleclick)
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive)
        table.setColumnWidth(0, 60)
        table.setColumnWidth(1, 100)
        table.setColumnWidth(2, 80)
        table.setColumnWidth(3, 100)
        table.setColumnWidth(4, 80)
        table.setColumnWidth(5, 70)
        table.setColumnWidth(6, 70)
        table.setColumnWidth(7, 90)
        table.setColumnWidth(8, 70)
        table.setColumnWidth(9, 110)
        table.setColumnWidth(10, 70)
        table.setColumnWidth(11, 60)
        table.setColumnWidth(12, 60)
        table.setColumnWidth(13, 180)
        self.channel_table = table

    def _create_progressbar(self):
        """在频道表格上方创建进度条；中央布局：进度条 → 表格。"""
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.channel_table)
        self.setCentralWidget(central)

    def _create_logbox(self):
        """在频道表格下方、状态栏上方创建只读日志框。"""
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(120)
        self.log_box.setPlaceholderText("日志输出…")

        # 日志框加到中央部件的垂直布局末尾（频道表格之后）
        self.centralWidget().layout().addWidget(self.log_box)

    def log(self, message):
        """向日志框追加一行带时间戳的信息。"""
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText("[%s] %s" % (stamp, message))

    def _create_statusbar(self):
        self.statusBar().showMessage("由BH1RIO编写")

    # ---------------- 关于 ----------------
    def _on_about(self):
        """显示关于窗体。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("关于")
        dlg.setModal(True)
        dlg.setMinimumWidth(400)

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        title = QLabel("宝锋 UV-5R Mini 写频软件")
        f = title.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        title.setFont(f)

        info = QLabel(
            "作者：BH1RIO\n\n"
            "本软件是宝锋 UV-5R Mini 手持机的电脑编程软件（CPS），支持：\n"
            "　· 从电台读取 / 向电台写入 999 个频道\n"
            "　· 打开 / 保存频道镜像文件\n"
            "　· 编辑频道频率、哑音、功率、信令码等参数\n\n"
            "技术栈：Python 3 · PySide6 (Qt) · pyserial")
        info.setWordWrap(True)

        btn = QPushButton("确定")
        btn.clicked.connect(dlg.accept)

        layout.addWidget(title)
        layout.addWidget(info)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.setLayout(layout)
        dlg.exec()

    # ---------------- 打开镜像 ----------------
    def _on_open_image(self):
        """打开镜像文件，加载到频道容器并解析、显示到频道列表。

        支持两种文件：
        - 完整电台镜像（33344 字节）：截取 0x0000 起的频道区；
        - 纯频道区数据（32 字节整数倍，如保存镜像产生的 31968 字节文件）。
        """
        path, _sel = QFileDialog.getOpenFileName(
            self, "打开镜像", "uv5r_mini.img",
            "宝锋 UV-5R Mini 镜像 (*.img *.bin);;所有文件 (*.*)")
        if not path:
            self.log("打开中止（用户取消）")
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            self.log("打开失败：无法读取文件（%s）" % exc)
            self.statusBar().showMessage("打开失败")
            return
        self.log("打开镜像：%s（%d 字节）" % (path, len(data)))
        try:
            if len(data) == MEM_TOTAL:
                self._full_image = bytes(data)
                chan = channel_image(data)
                self.log("识别为完整电台镜像，截取频道区 0x0000 起 %d 字节" % len(chan))
            elif len(data) % 32 == 0 and len(data) > 0:
                self._full_image = None
                chan = bytes(data)
                self.log("识别为频道区数据，共 %d 个频道" % (len(data) // 32))
            else:
                raise ValueError(
                    "长度 %d 既不是完整镜像 %d 字节，也不是 32 字节的整数倍" % (len(data), MEM_TOTAL))
            self.channels.image = chan          # setter 自动触发 decode()
        except (ValueError, RadioError) as exc:
            self.log("解析失败：%s" % exc)
            self.statusBar().showMessage("打开失败")
            return
        self.log("解析完成：%d 个频道，填充频道列表表格" % self.channels.count)
        self._log_channel_summary()
        self._populate_channel_table()
        self.progress_bar.setValue(100)
        self.log("打开镜像完成：共 %d 个频道" % self.channels.count)
        self.statusBar().showMessage("已打开 %s（%d 个频道）"
                                    % (os.path.basename(path), self.channels.count))

    # ---------------- 保存镜像 ----------------
    def _on_save_image(self):
        """把当前频道容器数据编码后保存为文件。

        若之前从电台读取过完整镜像，则把重新编码的频道区回填到完整镜像
        （保留 VFO/设置等非频道区），保证 .img 与原电台镜像同构（33344 字节）。
        """
        if not self.channels:
            self.log("保存中止：容器内没有频道数据")
            return
        try:
            encoded = self.channels.encode()
        except ValueError as exc:
            self.log("频道编码失败：%s" % exc)
            return
        self.log("编码频道区：%d 个频道 × %d 字节 = %d 字节"
                 % (self.channels.count, 32, len(encoded)))

        data = encoded
        if self._full_image is not None and len(encoded) == CHANNEL_IMAGE_SIZE:
            full = bytearray(self._full_image)
            full[:CHANNEL_IMAGE_SIZE] = encoded
            data = bytes(full)
            self.log("已把频道区回填到完整镜像（%d 字节，保留非频道区）" % len(full))
        elif self._full_image is not None:
            self.log("注意：频道区 %d 字节与镜像频道区 %d 字节不一致，仅保存频道数据"
                     % (len(encoded), CHANNEL_IMAGE_SIZE))

        path, _sel = QFileDialog.getSaveFileName(
            self, "保存镜像", "uv5r_mini.img",
            "宝锋 UV-5R Mini 镜像 (*.img *.bin);;所有文件 (*.*)")
        if not path:
            self.log("保存中止（用户取消）")
            return
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError as exc:
            self.log("保存失败：%s" % exc)
            self.statusBar().showMessage("保存失败")
            return
        self.log("保存完成：%s（%d 字节）" % (path, len(data)))
        self.statusBar().showMessage("已保存镜像到 %s" % path)

    def _on_channel_doubleclick(self, index):
        """双击某频道行 → 等同右键菜单的“编辑”动作。"""
        row = index.row()
        if 0 <= row < self.channels.count:
            self.channel_table.selectRow(row)
            self._on_edit_channel(row)

    # ---------------- 频道列表右键菜单 ----------------
    def _on_channel_menu(self, pos):
        """频道列表右键菜单：行移动 / 编辑 / 清空。"""
        row = self.channel_table.rowAt(pos.y())
        if row < 0:
            return
        self.channel_table.selectRow(row)

        menu = QMenu(self)
        a_front = menu.addAction("移到最前")
        a_up = menu.addAction("前移一行")
        a_down = menu.addAction("后移一行")
        a_end = menu.addAction("移到最后")
        menu.addSeparator()
        a_edit = menu.addAction("编辑")
        a_clear = menu.addAction("清空")

        chosen = menu.exec(self.channel_table.viewport().mapToGlobal(pos))
        if chosen is a_front:
            self._move_channel(row, self.channels.move_to_front)
        elif chosen is a_up:
            self._move_channel(row, self.channels.move_up)
        elif chosen is a_down:
            self._move_channel(row, self.channels.move_down)
        elif chosen is a_end:
            self._move_channel(row, self.channels.move_to_end)
        elif chosen is a_edit:
            self._on_edit_channel(row)
        elif chosen is a_clear:
            self._on_clear_channel(row)

    def _move_channel(self, row, move_fn):
        """调用容器移动方法，重刷表格并选中新位置。"""
        try:
            new_row = move_fn(row)
        except IndexError as exc:
            self.log(str(exc))
            return
        self._populate_channel_table()
        self.channel_table.selectRow(new_row)
        self.channel_table.scrollToItem(self.channel_table.item(new_row, 0))
        self.log("频道 %d → 位置 %d" % (row + 1, new_row + 1))

    def _on_edit_channel(self, row):
        """打开编辑对话框，确定后刷新表格。"""
        ch = self.channels[row]
        dialog = ChannelEditDialog(ch, self)
        if dialog.exec_edited():
            self._populate_channel_table()
            self.channel_table.selectRow(row)
            self.log("已编辑频道 %d" % ch.number)

    def _on_clear_channel(self, row):
        """清空选中频道的全部设置（复位为空频道）。"""
        ch = self.channels[row]
        ans = QMessageBox.question(
            self, "清空频道",
            "确定清空频道 %d 的全部设置吗？此操作不可撤销。" % ch.number,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        reset_channel_empty(ch)
        self._populate_channel_table()
        self.channel_table.selectRow(row)
        self.log("已清空频道 %d" % ch.number)

    # ---------------- 从电台读取 ----------------
    def _on_read_from_radio(self):
        """弹出串口选择对话框，选定后后台读取电台频道数据并显示。"""
        if self._worker is not None and self._worker.isRunning():
            self.log("已有读取任务正在进行，请稍候…")
            return

        dialog = SerialPortDialog(self)
        port = dialog.exec_selected()
        if not port:
            return

        self.act_read_radio.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log("正在从 %s 读取电台数据…" % port)
        self.statusBar().showMessage("正在从电台读取…")

        self._worker = ReadWorker(port, self)
        self._worker.progress.connect(self._on_read_progress)
        self._worker.log_message.connect(self.log)
        self._worker.finished_ok.connect(self._on_read_ok)
        self._worker.failed.connect(self._on_read_failed)
        self._worker.start()

    def _on_read_progress(self, cur, total):
        self.progress_bar.setValue(int(cur * 100 / total))

    def _on_read_ok(self, image):
        # 从电台读取已限定为 31968 字节频道区，不再有完整镜像
        self._full_image = None
        self.log("解析：电台返回 %d 字节（限定 0x0000 起频道区 %d 字节）"
                 % (len(image), CHANNEL_IMAGE_SIZE))
        try:
            chan_data = channel_image(image)
        except (RadioError, ValueError) as exc:
            self.log("解析失败：%s" % exc)
            self._on_read_failed(str(exc))
            return
        try:
            self.channels.image = chan_data
        except ValueError as exc:
            self.log("解码失败：%s" % exc)
            self._on_read_failed(str(exc))
            return
        self.log("解析：解码 %d 个频道完成，填充频道列表表格" % self.channels.count)
        self._log_channel_summary()
        self._populate_channel_table()
        self.progress_bar.setValue(100)
        self.act_read_radio.setEnabled(True)
        self.log("从电台读取完成，共 %d 个频道" % self.channels.count)
        self.statusBar().showMessage("读取完成，共 %d 个频道" % self.channels.count)

    # ---------------- 写入到电台 ----------------
    def _on_write_to_radio(self):
        """确认后把当前频道容器编码为 31968 字节频道区写入电台。"""
        if self._worker is not None and self._worker.isRunning():
            self.log("已有读/写任务正在进行，请稍候…")
            return
        if not self.channels:
            self.log("写入中止：容器内没有频道数据")
            return
        try:
            encoded = self.channels.encode()
        except ValueError as exc:
            self.log("频道编码失败：%s" % exc)
            return
        if len(encoded) != CHANNEL_IMAGE_SIZE:
            self.log("写入中止：频道数据 %d 字节 ≠ 频道区 %d 字节（必须恰好 %d 个频道）"
                     % (len(encoded), CHANNEL_IMAGE_SIZE, CHANNEL_COUNT))
            return
        ans = QMessageBox.question(
            self, "写入到电台",
            "确定将 %d 个频道（%d 字节频道区）写入电台吗？\n电台对应区域的当前设置将被覆盖。"
            % (self.channels.count, CHANNEL_IMAGE_SIZE),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            self.log("写入中止（用户取消）")
            return

        dialog = SerialPortDialog(self)
        port = dialog.exec_selected()
        if not port:
            return

        self.act_write_radio.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log("正在写入电台 %s（%d 字节频道区）…" % (port, CHANNEL_IMAGE_SIZE))
        self.statusBar().showMessage("正在写入电台…")

        self._worker = WriteWorker(port, encoded, self)
        self._worker.progress.connect(self._on_read_progress)
        self._worker.log_message.connect(self.log)
        self._worker.finished_ok.connect(self._on_write_ok)
        self._worker.failed.connect(self._on_write_failed)
        self._worker.start()

    def _on_write_ok(self, written):
        self.progress_bar.setValue(100)
        self.act_write_radio.setEnabled(True)
        self.log("写入电台完成，共 %d 字节" % written)
        self.statusBar().showMessage("写入完成，共 %d 字节" % written)

    def _on_write_failed(self, message):
        self.act_write_radio.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log("写入失败: %s" % message)
        self.statusBar().showMessage("写入失败")

    def _log_channel_summary(self):
        """解析完成后输出频道统计详情与前几个非空频道预览。"""
        all_ch = list(self.channels)
        non_empty = [c for c in all_ch if not _is_empty(c)]
        self.log("统计：共 %d 个频道，非空 %d 个，空 %d 个"
                 % (len(all_ch), len(non_empty), len(all_ch) - len(non_empty)))
        if not non_empty:
            self.log("（全部为空频道）")
            return
        freqs = [c.rxfreq for c in non_empty]
        self.log("接收频率范围：%.4f – %.4f MHz" % (min(freqs) * 10 / 1e6,
                                                     max(freqs) * 10 / 1e6))
        tx_off_n = sum(1 for c in non_empty if is_tx_off(c.txfreq))
        self.log("发射：禁止 %d 个，设频 %d 个" % (tx_off_n, len(non_empty) - tx_off_n))
        rx_tone = sum(1 for c in non_empty if c.rxtone not in (0, 0xFFFF))
        tx_tone = sum(1 for c in non_empty if c.txtone not in (0, 0xFFFF))
        self.log("哑音：接收侧 %d 个，发射侧 %d 个" % (rx_tone, tx_tone))
        named = sum(1 for c in non_empty if _name_text(c.name))
        self.log("已命名频道：%d 个" % named)
        dtmf_n = sum(1 for c in non_empty if c.sqmode & 1)
        squelch_n = sum(1 for c in non_empty if (c.sqmode >> 1) & 1)
        self.log("接收侧解码：哑音解码 %d 个，DTMF 解码 %d 个"
                 % (squelch_n, dtmf_n))
        preview = non_empty[:5]
        self.log("前 %d 个非空频道预览：" % len(preview))
        for c in preview:
            tx = "关闭" if is_tx_off(c.txfreq) else _freq_text(c.txfreq)
            self.log("  频道 %d：%s / %s，RX 哑音 %s，TX 哑音 %s，%s" % (
                c.number, _freq_text(c.rxfreq), tx,
                decode_tone(c.rxtone) or "无", decode_tone(c.txtone) or "无",
                _name_text(c.name) or "（无名称）"))

    def _on_read_failed(self, message):
        self.act_read_radio.setEnabled(True)
        self.progress_bar.setValue(0)
        self.log("读取失败: %s" % message)
        self.statusBar().showMessage("读取失败")

    def _populate_channel_table(self):
        """把 channels 数据填充到频道列表表格。"""
        table = self.channel_table
        table.setRowCount(0)
        for ch in self.channels:
            row = table.rowCount()
            table.insertRow(row)
            if _is_empty(ch):
                # 空频道（全 0xFF）：只保留编号与“空”标记
                values = ["空", ""] + [""] * (len(CHANNEL_COLUMNS) - 3)
            else:
                # S-CODE 显示值 = 存储字节 % 20 + 1（官方 1–20，无“无”）
                scode = str(ch.scode % (S_CODE_RANGE[1] - S_CODE_RANGE[0] + 1)
                            + S_CODE_RANGE[0])
                pttid = (PTTID_NAMES[ch.pttid]
                         if ch.pttid < len(PTTID_NAMES) else "0x%02X" % ch.pttid)
                values = [
                    _freq_text(ch.rxfreq),
                    decode_tone(ch.rxtone) or "无",
                    "关闭" if is_tx_off(ch.txfreq) else _freq_text(ch.txfreq),
                    decode_tone(ch.txtone) or "无",
                    "高" if ch.lowpower == 0 else ("低" if ch.lowpower == 1 else ""),
                    "宽" if not ch.wide else "窄",
                    "开" if ch.sqmode & 1 else "关",
                    scode,
                    pttid,
                    "开" if ch.bcl else "关",
                    "跳过" if not ch.scan else "扫描",
                    "开" if ch.fhss else "关",
                    _name_text(ch.name),
                ]
            all_values = [str(ch.number)] + values
            for col, text in enumerate(all_values):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, col, item)
        self.channel_table.resizeColumnsToContents()
