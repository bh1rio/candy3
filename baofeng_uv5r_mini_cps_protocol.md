# Baofeng UV-5R Mini CPS 克隆协议分析

> 基于 Chirp 源码（`d:\github\kk7ds\chirp`）分析。
> 驱动文件：`chirp/drivers/baofeng_uv17Pro.py`（2544 行）
> 低层收发原语：`chirp/drivers/baofeng_common.py`
> 串口打开逻辑：`chirp/wxui/clone.py`

---

## 一、驱动位置与类层次

| 项目 | 位置 |
|---|---|
| 驱动文件 | `chirp/drivers/baofeng_uv17Pro.py`（2544 行） |
| UV-5R Mini 类 | `UV5RMini(UV17Pro)`（第 2346 行） |
| 基类 | `UV17Pro`（第 222 行）→ `BaofengCommonHT`（`baofeng_common.py`）→ `CloneModeRadio` |
| 低层收发原语 | `_rawsend` / `_rawrecv` / `_clean_buffer`（`baofeng_common.py` 第 36–70 行） |

关键点：**UV-5R Mini 与经典 UV-5R（`uv5r.py`）的协议完全不同**。Mini 版复用的是 Baofeng UV-17 系列（GPS 彩屏机）的克隆协议族，并额外支持蓝牙 BLE 传输。

---

## 二、串口通信参数

由 `wxui/clone.py` 第 161–170 行打开串口：

- **波特率**：`BAUD_RATE = 115200`（`UV17Pro` 类定义，第 232 行）
- **流控**：`HARDWARE_FLOW = False`（无 RTS/CTS）
- **超时**：打开时 0.25 s；收发过程中驱动内 `STIMEOUT = 1.5 s`（`baofeng_common.py` 第 31 行）
- **传输介质**：USB 虚拟串口 或 **蓝牙 BLE 虚拟串口**
  （`sync_in/sync_out` 中通过 `platform.is_ble_serial(pipe)` 判断，如 Linux 下 `/tmp/ttyBLE*`）

---

## 三、握手流程（`_do_ident`，第 111–140 行）

1. **清缓冲**：`_clean_buffer` — 设 5 ms 超时读走最多 256 字节残余数据，恢复 1.5 s 超时。
2. **发送 ident 魔串**（`_sendmagic`）：
   - UV-5R Mini 使用 `MSTRING_UV17PROGPS = b"PROGRAMCOLORPROU"`（16 字节 ASCII）
   - 电台须应答**指纹** `_fingerprint = b"\x06"`（1 字节 ACK，且应答开头必须匹配指纹，
     否则视为非本机型继续尝试下一魔串）
3. **握手成功后依次发送 `_magics` 序列**：

   | 序号 | 发送内容 | 期望应答 | 推测含义 |
   |---|---|---|---|
   | 1 | `b"\x46"`（'F'，1 字节） | 16 字节 | 查询固件版本 |
   | 2 | `b"\x4d"`（'M'，1 字节） | 15 字节 | 查询型号信息 |
   | 3 | 25 字节魔串 `53 45 4E 44 21 05 0D 01 01 01 04 11 08 05 0D 0D 01 11 0F 09 12 09 10 04 00`（即 `SEND!` + 校验数据） | 1 字节 | 进入编程模式/放行数据区 |

---

## 四、数据帧格式（`_make_frame` / `_make_read_frame`，第 489–503 行）

帧头固定 4 字节，大端序：

```
┌──────────┬───────────────────┬────────────┬────────────────┐
│ CMD 1B   │  地址 ADDR 2B (>H)│  长度 LEN 1B│  数据 LEN 字节  │
└──────────┴───────────────────┴────────────┴────────────────┘
```

代码：`frame = cmd + struct.pack(">i", addr)[2:] + struct.pack("B", length) [+ data]`

| 方向 | CMD | 说明 |
|---|---|---|
| 读请求 | `0x52`（'R'） | 4 字节，无数据段。例：`52 00 00 40` = 读 0x0000 起 0x40 字节 |
| 写请求 | `0x57`（'W'） | 4 字节头 + N 字节负载 |
| 读应答 | 电台返回 4 字节头 + 数据（共 `LEN+4` 字节，驱动直接丢弃前 4 字节头，未校验） |
| 写应答 | 1 字节 `0x06`（ACK）；非 0x06 即报错 "Bad ack" |

---

## 五、数据加密（`_crypt`，第 81–105 行）

UV-5R Mini 启用加密：`_uses_encr = True`，`_encrsym = 1`。

- 符号表 `tblEncrySymbol[1] = b"CO 7"`，即 4 字节循环密钥 **`0x43 0x4F 0x20 0x37`**
  （"C"、"O"、空格、"7"）
- 逐字节异或，密钥索引每字节 +1 模 4
- **跳过规则**：字节值为 `0x00`、`0xFF`、等于当前密钥符号、或等于该符号 `^0xFF` 时，
  **原样保留不加密**
- 解密 = 加密（XOR 对称）

---

## 六、内存区映射（UV-5R Mini 特有）

```python
MEM_STARTS = [0x0000, 0x9000, 0xA000]
MEM_SIZES  = [0x8040, 0x0040, 0x01C0]
MEM_TOTAL  = 0x8240   # 33344 字节
BLOCK_SIZE = 0x40     # USB/串口读块 = 64 字节
```

即电台内存空间共 3 段（0x0000–0x9040、0x9040 后空档、0xA000–0xA1C0），
克隆后的 `.img` 镜像为三段拼接的连续 33344 字节。

---

## 七、完整通信时序

### 下载（`_download`，第 141–180 行）

1. `_do_ident` 握手（见第三节）
2. 循环遍历 3 个内存段，每次：
   - 发送 `52 [addr_hi] [addr_lo] 40`
   - 接收 `0x44` 字节 → 去 4 字节头 → 用 "CO 7" 解密 0x40 字节
   - 追加到镜像缓冲
3. 得到 33344 字节镜像，交给 bitwise 结构解析

### 上传（UV5RMini 自有 `_upload`，第 2390–2464 行，覆写基类）

1. `sync_out` 先探测是否 BLE 通道；若是 BLE 则写块大小改为
   **`BLE_UP_BLOCK_SIZE = 0x80`（128 字节）**，否则 0x40
2. `_do_ident` 握手
3. 循环写块：
   - 从镜像取 `byte_count = min(块大小, 段剩余)` 字节
   - 不足块大小的尾块用 **`0xFF` 填充**至整块（利用加密跳过 0xFF 的特性，
     填充字节在电台中呈 0xFF）
   - 加密后组帧 `57 [addr_hi] [addr_lo] [len] [data]` 发送
   - 等待 1 字节 ACK，必须为 `0x06`，否则抛 `RadioError("Bad ack writing block")`
4. 全程通过 `status_fn` 上报进度（`cur/max = 已写字节/总字节`）

### 可靠性机制

- 无 CRC/重传，唯一校验是应答字节长度精确匹配
  （`_rawrecv` 要求读够字节数，否则报错）和写 ACK `0x06`
- 读应答头 4 字节不做内容校验
- 依赖 1.5 s 读超时捕获无应答（`RadioNoResponse`）

---

## 八、与经典 UV-5R 协议的主要差异

| 对比项 | UV-5R（`uv5r.py`） | UV-5R Mini（本驱动） |
|---|---|---|
| 握手 | 直接发魔串，单 ACK | 双段握手（ident 指纹 + F/M/SEND! 三命令） |
| 数据加密 | 无 | "CO 7" XOR 加密 |
| 帧长字段 | 4 字节头 | 相同 |
| 镜像大小 | 0x9040 | 0x8240（三段拼接） |
| 蓝牙 | 无 | BLE 通道，上传块 128B + 0xFF 填充 |
| 信道数 | 200 | **999** |
