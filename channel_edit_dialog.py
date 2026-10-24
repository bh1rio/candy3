# -*- coding: utf-8 -*-
"""频道编辑对话框。

按 UV5RMiniChannel 字段逐项编辑（频率、音调、模式、功率、
S-CODE、PTT ID、BCL、扫描、扰码、FHSS、名称），
确定后把值写回频道对象。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
                               QMessageBox, QVBoxLayout)

from uv5rmini_radio import DTCS_CODES, TX_OFF

# CTCSS 标准频率表（Hz）
CTCSS_HZ = (67.0, 71.9, 74.4, 77.0, 82.5, 88.5, 91.5, 94.5, 97.4, 100.0,
            103.5, 105.0, 107.2, 110.9, 114.8, 117.4, 120.95, 123.0, 125.1,
            127.3, 131.8, 135.5, 141.3, 146.1, 151.4, 156.7, 162.2, 167.9,
            171.4, 173.8, 177.8, 179.85, 183.5, 186.8, 189.9, 192.8, 196.6,
            199.5, 203.5, 210.7, 218.1, 225.7, 233.6, 241.6, 250.3, 254.1,
            255.1, 261.05, 263.65, 266.55, 270.85)


def _tone_combo():
    """构建音调下拉框：无 / CTCSS 频率 / DTCS 码（正常N、反向R）。

    itemData 存音调原始值。
    """
    combo = QComboBox()
    combo.addItem("无", 0)
    combo.addItem("— CTCSS 亚音频 —", None)
    for hz in CTCSS_HZ:
        combo.addItem("%g Hz" % hz, int(round(hz * 10)))
    combo.addItem("— DTCS 亚音 —", None)
    for i, code in enumerate(DTCS_CODES):
        combo.addItem("%dN" % code, i + 1)
    for i, code in enumerate(DTCS_CODES):
        combo.addItem("%dR" % code, i + 1 + 0x69)   # Chirp: index + 1 + 0x69
    return combo


def _set_tone(combo, val):
    """按原始值选中对应项；无匹配时追加自定义项。"""
    if val in (0, 0xFFFF):
        combo.setCurrentIndex(0)
        return
    for i in range(combo.count()):
        if combo.itemData(i) == val:
            combo.setCurrentIndex(i)
            return
    combo.addItem("0x%04X" % val, val)
    combo.setCurrentIndex(combo.count() - 1)


def _get_tone(combo):
    """读回音调原始值（空项 → 0）。"""
    return combo.currentData() or 0


def mhz_to_stored(mhz):
    """MHz → 存储值（=Hz/10，即 MHz×100000）。"""
    return int(round(mhz * 100000))


def stored_to_mhz(stored):
    """存储值 → MHz。"""
    return stored * 10 / 1e6


class ChannelEditDialog(QDialog):
    """编辑单个频道的全部字段。"""

    def __init__(self, channel, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.setWindowTitle("编辑频道 %d" % channel.number)
        self.setMinimumWidth(360)

        # ---- 频率 ----
        self.rx_spin = QDoubleSpinBox()
        self.rx_spin.setRange(0.1, 999.9999)
        self.rx_spin.setDecimals(4)
        self.rx_spin.setSingleStep(0.0005)
        self.rx_spin.setSuffix(" MHz")
        if channel.rxfreq == TX_OFF:
            # 空频道：避免显示被钳位的 999.9999，从最小值起让用户输入
            self.rx_spin.setValue(self.rx_spin.minimum())
        else:
            self.rx_spin.setValue(stored_to_mhz(channel.rxfreq))

        self.tx_spin = QDoubleSpinBox()
        self.tx_spin.setRange(0.1, 999.9999)
        self.tx_spin.setDecimals(4)
        self.tx_spin.setSingleStep(0.0005)
        self.tx_spin.setSuffix(" MHz")
        if channel.txfreq == TX_OFF:
            # 发射频率缺省 = 接收频率（新频道/原发射关闭）
            self.tx_spin.setValue(self.rx_spin.value())
            self._tx_defaulted = True
            self._tx_modified = False
        else:
            self.tx_spin.setValue(stored_to_mhz(channel.txfreq))
            self._tx_defaulted = False
            self._tx_modified = False
        self.tx_spin.textChanged.connect(
            lambda _t: setattr(self, "_tx_modified", True))

        # ---- 音调 ----
        self.rx_tone = _tone_combo()
        _set_tone(self.rx_tone, channel.rxtone)
        self.tx_tone = _tone_combo()
        _set_tone(self.tx_tone, channel.txtone)

        # ---- 接收侧解码（sqmode 复合字段：bit0=DTMF解码，bit1=哑音解码）----
        # 哑音解码由接收哑音自动派生（有哑音=开，无哑音=关），不提供单独设置项
        self.dtmf_decode_cb = QCheckBox("DTMF解码")
        self.dtmf_decode_cb.setChecked(channel.sqmode & 1)

        # ---- 枚举/位域 ----
        self.mode_cb = QComboBox()
        self.mode_cb.addItem("宽", 0)
        self.mode_cb.addItem("窄", 1)
        self.mode_cb.setCurrentIndex(1 if channel.wide else 0)

        self.power_cb = QComboBox()
        self.power_cb.addItem("高 (5W)", 0)
        self.power_cb.addItem("低 (1W)", 1)
        self.power_cb.setCurrentIndex(0 if channel.lowpower == 0 else 1)

        self.scode_cb = QComboBox()
        # 官方 CPS：S-CODE 选项 1–20（无“无”），存储字节 = 显示值 - 1
        for code in range(1, 21):
            self.scode_cb.addItem(str(code), code - 1)
        self.scode_cb.setCurrentIndex(channel.scode % 20)

        self.pttid_cb = QComboBox()
        for name, val in (("无", 0), ("按下PPT", 1),
                          ("松开PPT", 2), ("按下和松开PPT", 3)):
            self.pttid_cb.addItem(name, val)
        self.pttid_cb.setCurrentIndex(
            channel.pttid if 0 <= channel.pttid <= 3 else 0)

        self.bcl_cb = QCheckBox("遇忙锁定")
        self.bcl_cb.setChecked(bool(channel.bcl))

        self.scan_cb = QCheckBox("允许扫描（取消=跳过）")
        self.scan_cb.setChecked(bool(channel.scan))

        self.fhss_cb = QCheckBox("跳频")
        self.fhss_cb.setChecked(bool(channel.fhss))

        # ---- 名称 ----
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(12)
        self.name_edit.setPlaceholderText("总长不超过 12 字节（汉字=2，字母数字=1）")
        data = bytes(channel.name).rstrip(b"\x00").rstrip(b"\xff")
        try:
            self.name_edit.setText(data.decode("gb2312"))
        except UnicodeDecodeError:
            self.name_edit.setText(data.decode("latin-1", errors="replace"))

        # ---- 布局 ----
        form = QFormLayout()
        form.addRow(QLabel("编号"),
                    QLabel(str(channel.number)))
        form.addRow("接收频率", self.rx_spin)
        form.addRow("发射频率", self.tx_spin)
        form.addRow("接收哑音", self.rx_tone)
        form.addRow("DTMF解码", self.dtmf_decode_cb)
        form.addRow("发射哑音", self.tx_tone)
        form.addRow("功率", self.power_cb)
        form.addRow("带宽", self.mode_cb)
        form.addRow("信令码", self.scode_cb)
        form.addRow("PTT ID", self.pttid_cb)
        form.addRow("遇忙锁定", self.bcl_cb)
        form.addRow("扫描", self.scan_cb)
        form.addRow("跳频", self.fhss_cb)
        form.addRow("名称", self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

    # ---------------- 应用 ----------------
    def _apply_name(self):
        """把名称编辑内容编码为 12 字节；失败返回 False。"""
        text = self.name_edit.text().strip()
        if not text:
            self.channel.name = b"\xff" * 12
            return True
        try:
            data = text.encode("gb2312")
        except UnicodeEncodeError:
            data = text.encode("latin-1", errors="replace")
        # 长度校验：汉字 2 字节，字母数字 1 字节，合计不超过 12 字节
        total = sum(2 if ord(ch) > 127 else 1 for ch in text)
        if total > 12:
            QMessageBox.warning(
                self, "名称过长",
                "名称超过 12 字节（汉字按 2 字节、字母数字按 1 字节计），已取消修改。")
            return False
        self.channel.name = data.ljust(12, b"\xff")   # 名称填充 0xFF（与 Chirp 一致）
        return True

    def _normalize_unmanaged(self, ch):
        """把未管理字段归一化为电台空白频道模板（与 Chirp set_memory 一致：
        前 16 字节 0x00、名称区 0xFF）。

        新建/编辑一个原本全 0xFF 的空频道时，这些字节若残留 0xFF/3
        等无效值，电台会认为该频道无效而不生效。
        注：sqmode 已由本对话框显式管理（哑音解码/DTMF解码），不再归零。
        """
        ch.unknown1 = 0
        ch.unknown2 = 0
        ch.unknown7 = 0
        ch.unknown8 = 0
        ch.unknown3 = 0
        ch.unknown4 = 0
        ch.unknown5 = 0
        ch.unknown6 = 0

    def _on_accept(self):
        ch = self.channel
        if not self._apply_name():
            return
        self._normalize_unmanaged(ch)
        ch.rxfreq = mhz_to_stored(self.rx_spin.value())
        # 发射频率：未被编辑且原为缺省 → 跟随接收频率；否则保留编辑值
        if self._tx_defaulted and not self._tx_modified:
            ch.txfreq = ch.rxfreq
        else:
            ch.txfreq = mhz_to_stored(self.tx_spin.value())
        ch.rxtone = _get_tone(self.rx_tone)
        ch.txtone = _get_tone(self.tx_tone)
        # sqmode 复合字段：bit0=DTMF解码（手动设置），bit1=哑音解码
        # （随接收哑音自动派生：有哑音=开，无哑音=关）
        rxt = _get_tone(self.rx_tone)
        ch.sqmode = ((1 if self.dtmf_decode_cb.isChecked() else 0)
                     | ((1 if rxt not in (0, None) else 0) << 1))
        ch.wide = self.mode_cb.currentData()
        ch.lowpower = self.power_cb.currentData()
        ch.scode = self.scode_cb.currentData()
        ch.pttid = self.pttid_cb.currentData()
        ch.bcl = 1 if self.bcl_cb.isChecked() else 0
        ch.scan = 1 if self.scan_cb.isChecked() else 0
        # 扰码不在界面显示：保留原值不变
        ch.fhss = 1 if self.fhss_cb.isChecked() else 0
        self.accept()

    def exec_edited(self):
        """模态执行；返回 True 表示已确定并写回频道对象。"""
        return self.exec() == QDialog.DialogCode.Accepted
