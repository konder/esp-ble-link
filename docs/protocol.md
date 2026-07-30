# 线协议

两侧必须一致的部分。改这里就是改兼容性。

## 传输层:Nordic UART Service

| 角色 | UUID | 属性 |
|---|---|---|
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` | |
| RX(中心 → 外设) | `6E400002-…` | `WRITE` \| `WRITE_NR` |
| TX(外设 → 中心) | `6E400003-…` | `NOTIFY` |

用 NUS 是因为它是事实标准 —— 现成的调试 app、别人的中枢都认这套。
换自定义 UUID 也行(`LinkConfig` 和 `DeviceConfig` 都能配),但两侧要一起换。

外设**不请求连接参数**,只在广播里给建议值 `minPreferred=0x06 / maxPreferred=0x12`
(1.25ms 为单位,即 7.5ms / 22.5ms)。原因见 [pitfalls.md A1](pitfalls.md#a1)。

## 组帧:分隔符分隔的 UTF-8 文本

一条消息 = 一行,以 `\n` 结尾。`\r` 也当分隔符处理(容忍 CRLF)。

- **中心 → 外设**:整行按 `min(180, maximumWriteValueLength)` 分片,
  逐片 `.withResponse` 写,**下一片只在收到 ATT ack 后发**。
- **外设 → 中心**:按协商 `ATT_MTU - 3` 分片 notify,片间 `delay(4)`;
  自动补分隔符。

外设侧把收到的字节推进环形缓冲,由主循环按分隔符重组。

### 长度约束(容易出事的地方)

| 参数 | 默认 | 含义 |
|---|---|---|
| `LinkConfig.rxRingBytes` | 2048 | 外设接收环形缓冲。**真正的单行上限** |
| `LinkConfig.maxFrameBytes` | 4096 | 单条重组消息上限,超了整条丢弃 |
| host `DEFAULT_LINE_LIMIT` | 1843 | = `rxRingBytes × 0.9`,留 10% 余量 |

**限的是字节不是字符。** 中文 3 字节/字。host 侧 `fit_line()` 会按字节限长并在
字符边界截断;自己拼行的话务必也这么做。

改了 `rxRingBytes` 记得同步 host 侧:`fit_line(obj, limit_for_ring(你的值))`。

## 应用层:JSON,一行一个对象

链路层不认识 JSON —— 它只管分隔符。下面是**约定**,不是强制,但 host 的
`RetainedChannel` 和示例固件都按这个来。

约定用 `t` 字段做类型分发:

```jsonc
// 中心 → 外设
{"t": "state", "rev": 12, ...}          // retained 状态,重连自动补推
{"t": "ev", "live": true, "msg": "…"}   // 事件;live=false 表示这是重连补发的
{"t": "cmd", "cmd": "ota"}              // 指令

// 外设 → 中心
{"t": "stats", "rx_dropped": 0, ...}    // 遥测
{"t": "echo", ...}                      // 示例固件的回显
```

### `rev` 字段的作用

keepalive 会**反复重发同一帧 retained 状态**(这是唯一能探出链路死活的手段,
见 [pitfalls.md C4](pitfalls.md#c4))。设备侧必须能识别「内容没变」并跳过重绘,
否则墨水屏之类会被刷爆。约定用一个单调递增的 `rev`:收到时 `rev` 没变就只更新
内存、不重绘。

### `live` 字段的作用

重连后 `RetainedChannel` 会重放历史事件,重放时打上 `live: false`。
设备侧据此**只把它放进列表,不蜂鸣、不弹全屏卡** —— 否则设备一重启就会把过去
八条通知重新提醒一遍。

---

## helper 邮箱协议(Python ↔ Swift)

这层是本框架内部的,但排查时要看得懂。

### 命令 `<session-dir>/commands/{seq:08d}.json`

Python 写(`.tmp` → `os.replace` 原子落盘),helper 读后删除。文件名补零到 8 位,
helper 靠字典序保证 FIFO。

```jsonc
{"seq": 1, "op": "write_json", "line": "{\"t\":\"ev\"}"}
{"seq": 2, "op": "ping"}       // ⚠️ 只证明 helper 活着,探不到 BLE 链路
{"seq": 3, "op": "shutdown"}
```

### 事件 `<session-dir>/events.jsonl`

helper 追加,Python 按字节偏移 tail(容忍截断与半行)。

| event | 字段 | 说明 |
|---|---|---|
| `launch` | `pid` | 进程起来了 |
| `central_created` | | `CBCentralManager` 构造完成 |
| `central_state` | `value` | `CBManagerState` 原始值。**5 = poweredOn** |
| `scan_started` | | 开始扫描 |
| `discovered` | `identifier` `name` `local_name` `rssi` | 按 identifier 去重,每台只报一次 |
| `connect_started` | `identifier` `name` `rssi` | 匹配上了,发起连接 |
| `connected_transport` | `name` | `didConnect` —— 链路通了但还不能用 |
| `connected` | `identifier` `name` `chunk` | 订阅成功,**这才算可用** |
| `disconnected` | `name` `error` | 终态,helper 随即自我终结 |
| `notification` | `line` | 设备 TX 上来的一行 |
| `ack` | `seq` | 命令完成 |
| `command_error` | `seq` `message` | 单条命令失败 |
| `error` | `message` | 致命/权限/扫描超时 |

健康的冷启动序列:

```
launch → central_created → central_state:5 → scan_started
       → discovered… → connect_started → connected_transport → connected
```

卡在哪一步直接对应哪类问题,见 [pitfalls.md B1](pitfalls.md#b1)。
