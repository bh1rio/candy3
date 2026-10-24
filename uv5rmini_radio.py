# -*- coding: utf-8 -*-
"""宝锋 UV-5R Mini CPS 串口通信层。

实现自 Chirp 驱动（chirp/drivers/baofeng_uv17Pro.py 的 UV5RMini 类）：

- 握手：ident 魔串 "PROGRAMCOLORPROU" + 指纹 0x06，
  随后依次发送 F(16B)/M(15B)/SEND!(1B) 三命令；
- 数据帧：CMD 1B + 地址 2B(大端) + 长度 1B + 数据；
  读请求 CMD=0x52('R')，读应答 = 4 字节头 + 数据；
- 加密："CO 7" 四字节循环 XOR，跳过 0x00 / 0xFF /
  密钥符号本身 / 符号^0xFF 的字节（解密=加密）；
- 内存区：3 段拼接，共 0x8240(33344) 字节；
- 频道区：镜像偏移 0x0000 起 999 个频道 × 32 字节 = 0x7CE0(31968) 字节。

本实现将读取/写入的数据均限定在 31968 字节频道区内（不读写其他内存区）：
- 下载：读 0x0000 起 31968 字节，返回时截断；
- 上传：写 31968 字节，每块等待 0x06 ACK。

传输层始终按整块 0x40（64 字节）收发（与 Chirp 验证过的行为一致）：
频道区非 64 的整数倍时，尾块按整块读（多余 32 字节丢弃）/按整块写（尾部 0xFF 填充）。
"""

import struct

import serial
from uv5rmini_channels import TX_OFF

# ---------------- 协议常量 ----------------
BAUD_RATE = 115200
STIMEOUT = 1.5                 # 串口读写超时（秒）
CLEAN_TIMEOUT = 0.005          # 清缓冲时的短超时（秒）
CLEAN_MAX_BYTES = 256          # 清缓冲最多读取的字节数

# 握手
IDENT = b"PROGRAMCOLORPROU"    # ident 魔串
FINGERPRINT = b"\x06"          # ident 应答指纹（1 字节 ACK）
MAGICS = [(b"\x46", 16),       # 查询固件版本，期望 16 字节
          (b"\x4d", 15),       # 查询型号信息，期望 15 字节
          (b"\x53\x45\x4e\x44\x21\x05\x0D\x01\x01\x01\x04\x11\x08\x05"
           b"\x0D\x0D\x01\x11\x0F\x09\x12\x09\x10\x04\x00", 1)]  # 进入编程模式

# 内存区映射（UV-5R Mini 特有）
MEM_STARTS = [0x0000, 0x9000, 0xA000]
MEM_SIZES = [0x8040, 0x0040, 0x01C0]
MEM_TOTAL = 0x8240             # 33344 字节（仅文档参考，本实现不读写）
BLOCK_SIZE = 0x40              # 块大小上限 64 字节

# 频道区（内存地址 0x0000 起）——读写均限定在此区间
CHANNEL_REGION_START = 0x0000
CHANNEL_SIZE = 32
CHANNEL_COUNT = 999
CHANNEL_IMAGE_SIZE = CHANNEL_SIZE * CHANNEL_COUNT   # 0x7CE0 = 31968 字节

# 发射禁止/空频道标记 TX_OFF 定义在 uv5rmini_channels（全 0xFF 频率的解码值）

# DTCS 标准码表（与 Chirp chirp_common.DTCS_CODES 一致）
DTCS_CODES = (
    23,  25,  26,  31,  32,  36,  43,  47,  51,  53,  54,
    65,  71,  72,  73,  74,  114, 115, 116, 122, 125, 131,
    132, 134, 143, 145, 152, 155, 156, 162, 165, 172, 174,
    205, 212, 223, 225, 226, 243, 244, 245, 246, 251, 252,
    255, 261, 263, 265, 266, 271, 274, 306, 311, 315, 325,
    331, 332, 343, 346, 351, 356, 364, 365, 371, 411, 412,
    413, 423, 431, 432, 445, 446, 452, 454, 455, 462, 464,
    465, 466, 503, 506, 516, 523, 526, 532, 546, 565, 606,
    612, 624, 627, 631, 632, 654, 662, 664, 703, 712, 723,
    731, 732, 734, 743, 754,
)


def is_tx_off(txfreq):
    """判定发射禁止：txfreq 为 0（全 0x00）或 TX_OFF（全 0xFF）。"""
    return txfreq in (0, TX_OFF)


def decode_tone(val):
    """音调原始值 → 显示文本（对应 Chirp decode_tone）。

    - 0 / 0xFFFF：无音调（空串）
    - >= 0x0258：CTCSS，频率 = val/10 Hz，如 851 → "85.1"
    - 其余非零：DTCS 码，val > 0x69 为反向(R)，否则正常(N)
    """
    if val in (0, 0xFFFF):
        return ""
    if val >= 0x0258:
        return "%.1f" % (val / 10)
    try:
        if val > 0x69:
            return "%dR" % DTCS_CODES[val - 0x6A]
        return "%dN" % DTCS_CODES[val - 1]
    except IndexError:
        # 非法 DTCS 索引（电台存了坏数据）：原样显示十六进制
        return "0x%04X" % val

# 加密密钥表 tblEncrySymbol[1] = "CO 7"
_ENC_KEY = b"CO 7"


class RadioError(Exception):
    """电台通信错误（对应 Chirp 的 RadioError / RadioNoResponse）。"""


def _crypt(buffer):
    """按 "CO 7" 四字节循环密钥对数据做 XOR 加/解密（对称）。

    跳过规则：密钥符号为空格、或字节为 0x00/0xFF/密钥符号/符号^0xFF
    时原样保留不加密。
    """
    out = bytearray()
    key = _ENC_KEY
    for i, b in enumerate(buffer):
        sym = key[i % 4]
        if sym != 0x20 and b not in (0x00, 0xFF, sym, sym ^ 0xFF):
            b ^= sym
        out.append(b)
    return bytes(out)


def _make_read_frame(addr, length):
    """组装读请求帧：0x52 + 2 字节大端地址 + 1 字节长度。"""
    return b"\x52" + struct.pack(">H", addr) + struct.pack("B", length)


def _make_write_frame(addr, data):
    """组装写请求帧：0x57 + 2 字节大端地址 + 1 字节长度 + 数据。"""
    return (b"\x57" + struct.pack(">H", addr) + struct.pack("B", len(data))
            + data)


def channel_image(image):
    """从完整电台镜像中切出频道区二进制数据。

    频道区位于镜像偏移 0x0000 起共 999 × 32 字节。
    """
    if len(image) < CHANNEL_IMAGE_SIZE:
        raise RadioError(
            "镜像长度 %d 不足频道区大小 %d" % (len(image), CHANNEL_IMAGE_SIZE))
    return bytes(image[:CHANNEL_IMAGE_SIZE])


class UV5RMiniRadio:
    """宝锋 UV-5R Mini 串口克隆接口。

    用法::

        radio = UV5RMiniRadio("COM3")
        try:
            image = radio.download(progress_cb=lambda cur, max: ...)
        finally:
            radio.close()
    """

    def __init__(self, port, baudrate=BAUD_RATE, timeout=STIMEOUT):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.pipe = serial.Serial(
            port, baudrate=baudrate, timeout=timeout,
            write_timeout=timeout, rtscts=False, dsrdtr=False)

    # ---------------- 底层收发 ----------------
    def _rawsend(self, data):
        try:
            self.pipe.write(data)
        except Exception as exc:
            raise RadioError("向电台发送数据失败: %s" % exc) from exc

    def _rawrecv(self, amount):
        """精确读取 amount 字节，读不够即报错。"""
        data = b""
        try:
            while len(data) < amount:
                chunk = self.pipe.read(amount - len(data))
                if not chunk:
                    break
                data += chunk
        except Exception as exc:
            raise RadioError(
                "读取电台数据出错，请检查连接线缆: %s" % exc) from exc
        if len(data) != amount:
            raise RadioError(
                "电台无响应（期望 %d 字节，实际 %d 字节）" % (amount, len(data)))
        return data

    def _clean_buffer(self):
        """清走串口中的残余数据。"""
        self.pipe.timeout = CLEAN_TIMEOUT
        junk = self.pipe.read(CLEAN_MAX_BYTES)
        self.pipe.timeout = self.timeout
        return junk

    # ---------------- 握手 ----------------
    _MAGIC_DESCS = ["查询固件版本", "查询型号信息", "进入编程模式（SEND! 25 字节）"]

    def _ident(self, log=None):
        """执行握手：魔串 + 指纹 + F/M/SEND! 三命令，逐步记录日志。"""
        def L(msg):
            if log:
                log(msg)
        L("握手：清串口缓冲后发送 ident 魔串 %r（%d 字节）" % (IDENT.decode(), len(IDENT)))
        self._clean_buffer()
        self._rawsend(IDENT)
        ack = self._rawrecv(len(FINGERPRINT))
        if not ack.startswith(FINGERPRINT):
            L("握手失败：应答 %r 与指纹 %r 不匹配，可能不是 UV-5R Mini" % (ack, FINGERPRINT))
            raise RadioError(
                "电台应答 %r 与指纹 %r 不匹配，可能不是 UV-5R Mini"
                % (ack, FINGERPRINT))
        L("握手：电台应答指纹 0x%02X，机型识别成功" % ack[0])
        for desc, (magic, resplen) in zip(self._MAGIC_DESCS, MAGICS):
            L("握手：发送 %s（%d 字节）" % (desc, len(magic)))
            self._rawsend(magic)
            resp = self._rawrecv(resplen)
            L("握手：收到应答 %d 字节：%s" % (resplen, resp[:12].hex(" ") + (" …" if len(resp) > 12 else "")))
        L("握手完成，电台已进入编程模式")

    # ---------------- 下载（数据限定 31968 字节频道区） ----------------
    def download(self, progress_cb=None, log_cb=None):
        """从电台下载频道区（31968 字节，0x0000 起），返回 bytes。

        只读取频道区数据，不读取其他内存区。传输按整块 0x40 进行
        （电台验证过的行为），尾块多读的 32 字节丢弃。

        progress_cb(cur, total)：每读完一块回调一次进度。
        log_cb(msg)：逐条详细日志（握手、逐块、完成）。
        """
        def L(msg):
            if log_cb:
                log_cb(msg)
        self._ident(L)
        region_end = CHANNEL_REGION_START + CHANNEL_IMAGE_SIZE
        total_blocks = (CHANNEL_IMAGE_SIZE + BLOCK_SIZE - 1) // BLOCK_SIZE
        L("开始读取频道区：地址 0x%04X 起 %d 字节 / %d 块（块大小 0x%02X，加密密钥 CO 7）" % (
            CHANNEL_REGION_START, CHANNEL_IMAGE_SIZE, total_blocks, BLOCK_SIZE))
        data = bytearray()
        cur = 0
        for addr in range(CHANNEL_REGION_START, region_end, BLOCK_SIZE):
            byte_count = min(BLOCK_SIZE, region_end - addr)
            L("  读块 0x%04X（需 %d 字节）…" % (addr, byte_count))
            self._rawsend(_make_read_frame(addr, BLOCK_SIZE))
            block = self._rawrecv(BLOCK_SIZE + 4)
            data += _crypt(block[4:])[:byte_count]
            cur += 1
            if progress_cb:
                progress_cb(cur, total_blocks)
        L("读取完成：频道区共 %d 字节（已逐字节 CO 7 解密）" % len(data))
        return bytes(data)

    # ---------------- 上传（数据限定 31968 字节频道区） ----------------
    def upload(self, image, progress_cb=None, log_cb=None):
        """把 31968 字节频道区数据写入电台（0x0000 起）。

        只写入频道区数据，不写入其他内存区。传输按整块 0x40 进行
        （电台验证过的行为）：尾块 32 字节数据 + 32 字节 0xFF 填充成整块
        发送（0xFF 在电台中呈空白，与 Chirp 的填充策略一致）。
        每块等待 1 字节 0x06 ACK。

        image：长度必须恰为 31968 字节，否则抛 RadioError。
        """
        def L(msg):
            if log_cb:
                log_cb(msg)
        if len(image) != CHANNEL_IMAGE_SIZE:
            raise RadioError(
                "写入数据长度 %d 字节，不等于频道区大小 %d 字节（999 × 32）"
                % (len(image), CHANNEL_IMAGE_SIZE))
        self._ident(L)
        region_end = CHANNEL_REGION_START + CHANNEL_IMAGE_SIZE
        total_blocks = (CHANNEL_IMAGE_SIZE + BLOCK_SIZE - 1) // BLOCK_SIZE
        L("开始写入频道区：地址 0x%04X 起 %d 字节 / %d 块（块大小 0x%02X，加密密钥 CO 7）" % (
            CHANNEL_REGION_START, CHANNEL_IMAGE_SIZE, total_blocks, BLOCK_SIZE))
        off = 0
        cur = 0
        for addr in range(CHANNEL_REGION_START, region_end, BLOCK_SIZE):
            byte_count = min(BLOCK_SIZE, region_end - addr)
            chunk = image[off:off + byte_count]
            off += byte_count
            if byte_count < BLOCK_SIZE:
                pad = BLOCK_SIZE - byte_count
                L("  写块 0x%04X（%d 字节数据 + %d 字节 0xFF 填充）…"
                  % (addr, byte_count, pad))
                chunk = chunk + b"\xff" * pad
            else:
                L("  写块 0x%04X（%d 字节）…" % (addr, byte_count))
            self._rawsend(_make_write_frame(addr, _crypt(chunk)))
            ack = self._rawrecv(1)
            if ack != b"\x06":
                raise RadioError("写块 0x%04X 应答错误：%r（期望 0x06）" % (addr, ack))
            cur += 1
            if progress_cb:
                progress_cb(cur, total_blocks)
        L("写入完成：频道区共 %d 字节，全部块应答 0x06" % off)
        return off

    # ---------------- 生命周期 ----------------
    def close(self):
        try:
            self.pipe.close()
        except Exception:
            pass
