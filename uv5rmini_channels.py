# -*- coding: utf-8 -*-
"""宝锋 UV-5R Mini 频道有序容器。

UV5RMiniChannels 是 UV5RMiniChannel 的有序容器，
维护频道的先后顺序，支持插入、删除、替换、遍历
以及顺序调整（前移一位、后移一位、移到最前、移到最后）。

说明：
- 容器只管理频道对象的顺序，不修改频道数据本身；
- 位置索引为 0 起始（支持负索引），越界抛出 IndexError；
- 容器只接受 UV5RMiniChannel 对象，否则抛出 TypeError；
- image 属性用于保存从电台读取的频道二进制数据；
- 设置 image 时自动触发解码（也可显式调用 decode()）：
  按每 32 字节一个频道拆解为 UV5RMiniChannel 序列，
  并替换容器内现有频道；解码只做机械拆解，不做任何语义解析；
- encode() 将容器内频道按顺序反向组装为写入电台的二进制数据
  （每频道 32 字节，与解码互为逆操作，同样只做机械组装）。
"""

import struct

from uv5rmini_channel import UV5RMiniChannel

# 单个频道在电台内存中的字节数（memory_obj 布局，见 uv5rmini_channel.py）
CHANNEL_SIZE = 32

# 频率四字节全 0xFF 经 _bcd_decode 解码后的值（每字节解为 165）。
# 电台用它表示“发射禁止”/空频道；编码时需还原为 0xFF×4（非合法 BCD 数）。
TX_OFF = 166666665


def _bcd_decode(data):
    """将 4 字节小端 BCD 解码为整数。

    每字节是一个 0-99 的打包 BCD 数字（高 4 位为十位，低 4 位为个位），
    字节序低位在前。例如 [0x00, 0x00, 0x50, 0x14] -> 14500000。
    """
    val = 0
    for byte in reversed(data[:4]):
        val = val * 100 + ((byte >> 4) * 10 + (byte & 0x0F))
    return val


def _bcd_encode(value):
    """将整数（0-99999999）编码为 4 字节小端 BCD（低位字节在前）。

    每字节是一个 0-99 的打包 BCD 数字（高 4 位为十位，低 4 位为个位）。
    例如 14500000 -> [0x00, 0x00, 0x50, 0x14]。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("BCD 值必须是整数，实际为: %r" % (value,))
    if not 0 <= value <= 99999999:
        raise ValueError("BCD 值超出范围 0-99999999: %r" % (value,))
    out = bytearray(4)
    for i in range(4):
        two = value % 100
        value //= 100
        out[i] = ((two // 10) << 4) | (two % 10)
    return bytes(out)


def _freq_bcd_encode(value):
    """频率存储值 → 4 字节。TX_OFF（全 0xFF 标记）还原为 0xFF×4，否则按 BCD 编码。"""
    if value == TX_OFF:
        return b"\xff" * 4
    return _bcd_encode(value)


def _check_range(field, value, high):
    """校验 value 为 [0, high] 内的整数（不含 bool），返回该值。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s 必须是整数，实际为: %r" % (field, value))
    if not 0 <= value <= high:
        raise ValueError("%s 超出范围 0-%d: %r" % (field, high, value))
    return value


def reset_channel_empty(channel):
    """将一个频道复位为空频道（对应电台中该 32 字节区全为 0xFF）。

    就地修改 channel，返回该对象。
    """
    channel.rxfreq = TX_OFF               # 编码时还原为 0xFF×4
    channel.txfreq = TX_OFF
    channel.rxtone = 0xFFFF
    channel.txtone = 0xFFFF
    channel.scode = 0xFF
    channel.pttid = 0xFF
    channel.unknown7 = 3
    channel.scramble = 3
    channel.unknown8 = 3
    channel.lowpower = 3
    channel.unknown1 = 1
    channel.wide = 1
    channel.sqmode = 3
    channel.bcl = 1
    channel.scan = 1
    channel.unknown2 = 1
    channel.fhss = 1
    channel.unknown3 = 0xFF
    channel.unknown4 = 0xFF
    channel.unknown5 = 0xFF
    channel.unknown6 = 0xFF
    channel.name = b"\xff" * 12
    return channel


class UV5RMiniChannels:
    """UV5RMiniChannel 的有序容器。"""

    def __init__(self, channels=None):
        self._channels = []
        self._image = None
        if channels is not None:
            for ch in channels:
                self.add(ch)

    # ---------------- 电台二进制数据 ----------------
    @property
    def image(self):
        """从电台读取的频道二进制数据（bytes）；尚未读取时为 None。"""
        return self._image

    @image.setter
    def image(self, data):
        if data is not None and not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(
                "image 必须是 bytes/bytearray/memoryview 或 None，实际为: %s"
                % type(data).__name__)
        self._image = bytes(data) if data is not None else None
        if self._image is not None:
            self.decode()

    # ---------------- 解码/编码 ----------------
    def decode(self):
        """将 image 解码为 UV5RMiniChannel 序列，替换容器内现有频道。

        image 每 32 字节对应一个频道（memory_obj 布局）。
        返回解码得到的频道列表。
        """
        if self._image is None:
            raise ValueError("尚未设置 image，无法解码")
        if len(self._image) % CHANNEL_SIZE != 0:
            raise ValueError(
                "image 长度必须是 %d 的整数倍，实际为 %d"
                % (CHANNEL_SIZE, len(self._image)))
        count = len(self._image) // CHANNEL_SIZE
        self._channels = [
            self._decode_channel(
                self._image[i * CHANNEL_SIZE:(i + 1) * CHANNEL_SIZE], i + 1)
            for i in range(count)
        ]
        return list(self._channels)

    @staticmethod
    def _decode_channel(block, number):
        """将 32 字节频道数据机械拆解为一个 UV5RMiniChannel。"""
        return UV5RMiniChannel(
            number=number,
            rxfreq=_bcd_decode(block[0:4]),
            txfreq=_bcd_decode(block[4:8]),
            rxtone=struct.unpack_from('<H', block, 8)[0],
            txtone=struct.unpack_from('<H', block, 10)[0],
            scode=block[12],
            pttid=block[13],
            # 偏移 0x0E 位域（MSB 优先）
            unknown7=(block[14] >> 6) & 0x3,
            scramble=(block[14] >> 4) & 0x3,
            unknown8=(block[14] >> 2) & 0x3,
            lowpower=block[14] & 0x3,
            # 偏移 0x0F 位域（MSB 优先）
            unknown1=(block[15] >> 7) & 1,
            wide=(block[15] >> 6) & 1,
            sqmode=(block[15] >> 4) & 0x3,
            bcl=(block[15] >> 3) & 1,
            scan=(block[15] >> 2) & 1,
            unknown2=(block[15] >> 1) & 1,
            fhss=block[15] & 1,
            unknown3=block[16],
            unknown4=block[17],
            unknown5=block[18],
            unknown6=block[19],
            name=bytes(block[20:32]),
        )

    def encode(self):
        """将容器内全部频道按顺序编码为写入电台的二进制数据。

        每频道 32 字节（memory_obj 布局），依次拼接。返回 bytes。
        """
        return b"".join(self._encode_channel(ch) for ch in self._channels)

    @staticmethod
    def _encode_channel(ch):
        """将一个 UV5RMiniChannel 反向组装为 32 字节。"""
        name = ch.name
        if not isinstance(name, (bytes, bytearray)) or len(name) != 12:
            raise ValueError("name 必须是 12 字节，实际为: %r" % (name,))

        rxtone = _check_range("rxtone", ch.rxtone, 0xFFFF)
        txtone = _check_range("txtone", ch.txtone, 0xFFFF)
        # 频率：0-99999999 合法 BCD，或 TX_OFF（全 0xFF 标记）
        for f in ("rxfreq", "txfreq"):
            v = getattr(ch, f)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError("%s 必须是整数，实际为: %r" % (f, v))
            if v != TX_OFF and not 0 <= v <= 99999999:
                raise ValueError("%s 超出范围: %r" % (f, v))
        scode = _check_range("scode", ch.scode, 0xFF)
        pttid = _check_range("pttid", ch.pttid, 0xFF)
        unknown3 = _check_range("unknown3", ch.unknown3, 0xFF)
        unknown4 = _check_range("unknown4", ch.unknown4, 0xFF)
        unknown5 = _check_range("unknown5", ch.unknown5, 0xFF)
        unknown6 = _check_range("unknown6", ch.unknown6, 0xFF)

        # 位域打包（MSB 优先，与解码一致）
        flags1 = (((ch.unknown7 & 0x3) << 6)
                  | ((ch.scramble & 0x3) << 4)
                  | ((ch.unknown8 & 0x3) << 2)
                  | (ch.lowpower & 0x3))
        flags2 = (((ch.unknown1 & 1) << 7)
                  | ((ch.wide & 1) << 6)
                  | ((ch.sqmode & 0x3) << 4)
                  | ((ch.bcl & 1) << 3)
                  | ((ch.scan & 1) << 2)
                  | ((ch.unknown2 & 1) << 1)
                  | (ch.fhss & 1))

        return (_freq_bcd_encode(ch.rxfreq)
                + _freq_bcd_encode(ch.txfreq)
                + struct.pack('<H', rxtone)
                + struct.pack('<H', txtone)
                + bytes([scode, pttid, flags1, flags2,
                         unknown3, unknown4, unknown5, unknown6])
                + bytes(name))

    # ---------------- 内部校验 ----------------
    @staticmethod
    def _check_channel(channel):
        if not isinstance(channel, UV5RMiniChannel):
            raise TypeError(
                "仅可存放 UV5RMiniChannel 对象，实际为: %s"
                % type(channel).__name__)

    def _check_index(self, index):
        n = len(self._channels)
        if index < 0:
            index += n
        if not 0 <= index < n:
            raise IndexError(
                "频道索引越界: %d（容器内共 %d 个频道）" % (index, n))
        return index

    # ---------------- 序列接口 ----------------
    @property
    def count(self):
        """容器内频道数量。"""
        return len(self._channels)

    def __len__(self):
        return len(self._channels)

    def __iter__(self):
        return iter(self._channels)

    def __contains__(self, channel):
        return channel in self._channels

    def __bool__(self):
        return bool(self._channels)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return self._channels[index]
        return self._channels[self._check_index(index)]

    def __setitem__(self, index, channel):
        self._check_channel(channel)
        self._channels[self._check_index(index)] = channel

    def __delitem__(self, index):
        del self._channels[self._check_index(index)]

    def __repr__(self):
        img = len(self._image) if self._image is not None else 0
        return "UV5RMiniChannels(count=%d, image=%d bytes)" % (len(self._channels), img)

    # ---------------- 频道增删 ----------------
    def add(self, channel):
        """在末尾追加一个频道，返回其位置索引。"""
        self._check_channel(channel)
        self._channels.append(channel)
        return len(self._channels) - 1

    def insert(self, index, channel):
        """在指定位置插入一个频道（该位置及之后的频道依次后移）。"""
        self._check_channel(channel)
        n = len(self._channels)
        if index < 0:
            index += n
        if not 0 <= index <= n:
            raise IndexError(
                "插入位置越界: %d（容器内共 %d 个频道）" % (index, n))
        self._channels.insert(index, channel)
        return index

    def get(self, index):
        """返回指定位置的频道。"""
        return self[index]

    def delete(self, index):
        """删除指定位置的频道，返回被删除的频道对象。"""
        return self._channels.pop(self._check_index(index))

    def clear(self):
        """清空容器。"""
        self._channels.clear()

    # ---------------- 顺序调整 ----------------
    def move_up(self, index):
        """将指定位置的频道前移一位，返回新位置索引。"""
        i = self._check_index(index)
        if i == 0:
            raise IndexError("首个频道无法再前移")
        ch = self._channels.pop(i)
        self._channels.insert(i - 1, ch)
        return i - 1

    def move_down(self, index):
        """将指定位置的频道后移一位，返回新位置索引。"""
        i = self._check_index(index)
        if i == len(self._channels) - 1:
            raise IndexError("末尾频道无法再后移")
        ch = self._channels.pop(i)
        self._channels.insert(i + 1, ch)
        return i + 1

    def move_to_front(self, index):
        """将指定位置的频道移到最前，返回新位置索引。"""
        i = self._check_index(index)
        ch = self._channels.pop(i)
        self._channels.insert(0, ch)
        return 0

    def move_to_end(self, index):
        """将指定位置的频道移到最后，返回新位置索引。"""
        i = self._check_index(index)
        ch = self._channels.pop(i)
        self._channels.append(ch)
        return len(self._channels) - 1
