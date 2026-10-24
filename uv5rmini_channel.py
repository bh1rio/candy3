# -*- coding: utf-8 -*-
"""宝锋 UV-5R Mini 单一频道数据对象。

UV5RMiniChannel 对应从电台读取并解析得到的一个频道数据，
只存储数据，不做任何解析性的工作（BCD 频率换算、
CTCSS/DTCS 音调解码、双工/偏移判定、名称解码等均由外部完成）。

字段与 Chirp 驱动（chirp/drivers/baofeng_uv17Pro.py）中
memory_obj 的 32 字节布局一一对应：

偏移  长度  字段        说明
----  ----  ----------  ------------------------------------------
0x00  4     rxfreq      lbcd[4]，接收频率原始值（存储值 = 频率Hz / 10）
0x04  4     txfreq      lbcd[4]，发射频率原始值（0xFF*4 表示发射禁止）
0x08  2     rxtone      ul16（小端），接收侧音调原始值
0x0A  2     txtone      ul16（小端），发射侧音调原始值
0x0C  1     scode       u8，S-CODE 索引（显示值 = 字节+1：0=S-CODE 1 … 19=S-CODE 20）
0x0D  1     pttid       u8，PTT ID
0x0E  1     位域(MSB优先) unknown7:2 | scramble:2 | unknown8:2 | lowpower:2
0x0F  1     位域(MSB优先) unknown1:1 | wide:1 | sqmode:2 | bcl:1 |
                        scan:1 | unknown2:1 | fhss:1
0x10  1     unknown3    u8
0x11  1     unknown4    u8
0x12  1     unknown5    u8
0x13  1     unknown6    u8
0x14  12    name        char[12]，名称原始字节（0x00/0xFF 填充）
"""

from dataclasses import dataclass, field


@dataclass
class UV5RMiniChannel:
    """宝锋 UV-5R Mini 单一频道数据对象（纯数据存储，无解析逻辑）。"""

    # 频道号（1 起始），即该频道在电台中的位置，不属于 32 字节数据本身
    number: int = 0

    # ---- 频率原始值（lbcd[4]，存储值 = 频率Hz / 10） ----
    rxfreq: int = 0
    txfreq: int = 0

    # ---- 音调原始值（ul16，小端） ----
    rxtone: int = 0
    txtone: int = 0

    # ---- 普通 u8 字段 ----
    scode: int = 0         # S-CODE 索引，显示值 = 字节 + 1（官方 1–20，无“无”）
    pttid: int = 0

    # ---- 偏移 0x0E 位域（MSB 优先） ----
    unknown7: int = 0    # 2 bit
    scramble: int = 0    # 2 bit
    unknown8: int = 0    # 2 bit
    lowpower: int = 0    # 2 bit

    # ---- 偏移 0x0F 位域（MSB 优先） ----
    unknown1: int = 0    # 1 bit
    wide: int = 0        # 1 bit
    sqmode: int = 0      # 2 bit，接收侧解码复合：bit0=DTMF解码，bit1=哑音(CTCSS/DTCS)解码
    bcl: int = 0         # 1 bit
    scan: int = 0        # 1 bit
    unknown2: int = 0    # 1 bit
    fhss: int = 0        # 1 bit

    # ---- 未知字节（原样保留，保证往返一致） ----
    unknown3: int = 0
    unknown4: int = 0
    unknown5: int = 0
    unknown6: int = 0

    # ---- 名称（12 字节原始数据） ----
    name: bytes = field(default_factory=lambda: b"\xff" * 12)
