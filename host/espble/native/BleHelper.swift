// esp-ble-link 的 macOS BLE 中枢 helper —— CoreBluetooth 中心,以子进程形式被 Python 驱动。
//
// ============================================================================
// 为什么中枢必须是「编译出来的 .app bundle」而不是 Python + bleak
// ============================================================================
// macOS 把蓝牙权限(TCC)授给**实际接触 CoreBluetooth 的那个 bundle 可执行文件**。
// Python 进程永远不可能是那个文件 —— 套什么壳都不行,exec 会替换镜像,TCC 跟着新镜像走。
// 这是「macOS BLE 中枢重连不可靠」的三条根因之一(另两条:外设连上瞬间请求连接参数、
// 在同一个 CBCentralManager 里反复 retry)。详见 docs/pitfalls.md。
//
// ============================================================================
// 与 Python 的接口是**文件邮箱**,不是 stdin/stdout
// ============================================================================
//   <session-dir>/commands/{seq:08d}.json   Python 写(.tmp → rename 原子落盘),本进程读后删除
//   <session-dir>/events.jsonl              本进程追加,Python 按字节偏移 tail
//
// 之所以不用管道:启动必须走 `open -g -j -n`(否则拿不到 bundle 身份),而 open
// 不会把管道交给子进程。文件邮箱是这个约束下的必然选择,顺带还能在事后翻日志。
//
// ============================================================================
// 恢复策略:任何失败都自我终结,由 Python 起全新进程
// ============================================================================
// macOS CoreBluetooth 的坏状态是**进程级**的。在同一个 CBCentralManager 里重试
// 只会越积越坏。所以连不上、掉线、扫描超时 —— 一律 terminate,让外面开新进程。
// 这个 helper 因此简单到几乎没有可坏的状态。
//
// 本文件是**通用**的:UUID、设备名、分片上限全走命令行参数,
// 不同设备共用同一份源码,只有 bundle id 与可执行文件名在构建期注入
// (见 build_helper.sh —— 每个 helper 必须有独立 bundle id,否则 LaunchServices
//  会把它们当成同一个 app,互相顶掉)。

import AppKit
import CoreBluetooth
import Foundation

private let defaultService = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
private let defaultRx      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
private let defaultTx      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

private struct Config {
    let sessionDir: URL
    let commandsDir: URL
    let eventsURL: URL
    let service: CBUUID
    let rxUUID: CBUUID
    let txUUID: CBUUID
    let deviceID: String?
    let deviceName: String?
    let namePrefix: String?
    let scanTimeout: Double
    let chunkCeiling: Int
    let acceptUnterminated: Bool

    static func parse() throws -> Config {
        var args = Array(CommandLine.arguments.dropFirst())
        func take(_ flag: String) -> String? {
            guard let idx = args.firstIndex(of: flag), idx + 1 < args.count else { return nil }
            let value = args[idx + 1]
            args.removeSubrange(idx...(idx + 1))
            return value
        }
        func flag(_ name: String) -> Bool {
            guard let idx = args.firstIndex(of: name) else { return false }
            args.remove(at: idx)
            return true
        }
        func nonEmpty(_ s: String?) -> String? {
            guard let s, !s.isEmpty else { return nil }
            return s
        }

        let acceptUnterminated = flag("--accept-unterminated")
        guard let session = take("--session-dir") else {
            throw NSError(domain: "espble", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Missing --session-dir",
            ])
        }
        let sessionDir = URL(fileURLWithPath: session, isDirectory: true)
        return Config(
            sessionDir: sessionDir,
            commandsDir: sessionDir.appendingPathComponent("commands", isDirectory: true),
            eventsURL: sessionDir.appendingPathComponent("events.jsonl", isDirectory: false),
            service: CBUUID(string: take("--service") ?? defaultService),
            rxUUID: CBUUID(string: take("--rx") ?? defaultRx),
            txUUID: CBUUID(string: take("--tx") ?? defaultTx),
            deviceID: nonEmpty(take("--device-id")),
            deviceName: nonEmpty(take("--device-name")),
            namePrefix: nonEmpty(take("--name-prefix")),
            scanTimeout: Double(take("--scan-timeout") ?? "") ?? 20.0,
            // 上游硬编码 180;macOS 实测 ATT_MTU 185 → 182 可用,留点余量。
            chunkCeiling: Int(take("--chunk-ceiling") ?? "") ?? 180,
            acceptUnterminated: acceptUnterminated
        )
    }
}

private struct CommandEnvelope: Decodable {
    let seq: Int
    let op: String
    let line: String?
}

final class AppDelegate: NSObject, NSApplicationDelegate, CBCentralManagerDelegate, CBPeripheralDelegate {
    private var config: Config!
    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var rxChar: CBCharacteristic?
    private var txChar: CBCharacteristic?
    private var commandTimer: Timer?
    private var scanStartedAt = Date()
    private var rxBuffer = Data()
    private var pendingChunks: [Data] = []
    private var activeSeq: Int?
    private var ready = false
    private var stopping = false
    // 每个 identifier 只报一次「发现」。对每个广播都 emit 会把日志刷爆;
    // 完全不报又分不清「扫描本身坏了」和「设备不在」—— 去重报一次两者兼得。
    private var seen = Set<String>()

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            config = try Config.parse()
            try FileManager.default.createDirectory(at: config.commandsDir, withIntermediateDirectories: true)
            try Data().write(to: config.eventsURL, options: .atomic)
        } catch {
            fputs("espble helper launch failed: \(error)\n", stderr)
            NSApp.terminate(nil)
            return
        }

        emit(["event": "launch", "pid": ProcessInfo.processInfo.processIdentifier])
        central = CBCentralManager(delegate: self, queue: nil)
        emit(["event": "central_created"])

        // 扫描超时的看门狗必须独立于 commandTimer:commandTimer 只在订阅成功后才启动,
        // 冷启动一个都没发现时它根本没跑起来,超时判断会永远不执行。
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.checkScanTimeout()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        commandTimer?.invalidate()
        commandTimer = nil
    }

    private func checkScanTimeout() {
        guard !stopping, central != nil, central.state == .poweredOn, peripheral == nil else { return }
        if Date().timeIntervalSince(scanStartedAt) > config.scanTimeout {
            emitError("timed out scanning for \(config.deviceName ?? config.namePrefix ?? "device")")
            stopping = true
            NSApp.terminate(nil)
        }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        emit(["event": "central_state", "value": central.state.rawValue])
        guard central.state == .poweredOn else {
            // unauthorized = TCC 没授权,重试一万次也没用。Python 侧据此停止重连
            // 并给出可操作的提示,而不是无脑刷屏。
            if central.state == .unauthorized {
                emitError("bluetooth unauthorized: 需在 系统设置 > 隐私与安全性 > 蓝牙 中允许本 app")
            } else if central.state == .unsupported || central.state == .poweredOff {
                emitError("bluetooth unavailable: state=\(central.state.rawValue)")
            }
            return
        }

        scanStartedAt = Date()
        // 全扫 + 按名字匹配,**不按 service UUID 过滤**。一张桌子上常有好几台
        // 都跑 NUS 的设备,按 service 过滤会连上别人的。service UUID 只用来
        // 收窄后面的 discoverServices。
        central.scanForPeripherals(withServices: nil, options: [
            CBCentralManagerScanOptionAllowDuplicatesKey: false,
        ])
        emit(["event": "scan_started"])
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let ident = peripheral.identifier.uuidString.uppercased()
        if seen.insert(ident).inserted {
            emit([
                "event": "discovered", "identifier": ident,
                "name": peripheral.name ?? "",
                "local_name": advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? "",
                "rssi": RSSI.intValue,
            ])
        }
        guard matches(peripheral: peripheral, advertisementData: advertisementData) else { return }
        self.peripheral = peripheral
        peripheral.delegate = self
        central.stopScan()
        emit([
            "event": "connect_started", "identifier": ident,
            "name": peripheral.name ?? "", "rssi": RSSI.intValue,
        ])
        central.connect(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        emit(["event": "connected_transport", "name": peripheral.name ?? ""])
        // ⚠️ 不做任何连接参数请求 —— 中心侧 CoreBluetooth 根本不暴露该 API,
        // 而外设侧(EspBleLink)也刻意没有 updateConnParams。参数全由系统协商。
        peripheral.discoverServices([config.service])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        emitError("connect failed: \(error?.localizedDescription ?? "unknown")")
        stopping = true
        NSApp.terminate(nil)      // 让 Python 起全新进程(坏状态是进程级的)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        ready = false
        rxChar = nil
        txChar = nil
        emit([
            "event": "disconnected", "name": peripheral.name ?? "",
            "error": error?.localizedDescription ?? "",
        ])
        stopping = true
        NSApp.terminate(nil)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error {
            emitError("discover services failed: \(error.localizedDescription)")
            return
        }
        peripheral.services?.forEach { service in
            peripheral.discoverCharacteristics([config.txUUID, config.rxUUID], for: service)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error {
            emitError("discover characteristics failed: \(error.localizedDescription)")
            return
        }
        service.characteristics?.forEach { characteristic in
            if characteristic.uuid == config.rxUUID { rxChar = characteristic }
            if characteristic.uuid == config.txUUID { txChar = characteristic }
        }
        if let txChar {
            peripheral.setNotifyValue(true, for: txChar)
        } else {
            emitError("TX characteristic not found")
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            emitError("start notify failed: \(error.localizedDescription)")
            return
        }
        guard characteristic.uuid == config.txUUID, characteristic.isNotifying else { return }
        ready = true
        if commandTimer == nil {
            commandTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                self?.pumpCommands()
            }
        }
        // 订阅成功才算「可用」—— didConnect 只是链路通了,还写不了东西。
        emit([
            "event": "connected",
            "identifier": peripheral.identifier.uuidString.uppercased(),
            "name": peripheral.name ?? "",
            "chunk": chunkSize(),
        ])
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            let seq = activeSeq ?? -1
            activeSeq = nil
            pendingChunks.removeAll()
            emit(["event": "command_error", "seq": seq, "message": error.localizedDescription])
            return
        }
        sendNextChunk()      // 严格 ACK 驱动:一次只有一片在飞
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil, let data = characteristic.value else { return }
        rxBuffer.append(data)
        while let newline = rxBuffer.firstIndex(of: 0x0A) {
            let lineData = rxBuffer[..<newline]
            rxBuffer.removeSubrange(...newline)
            guard !lineData.isEmpty else { continue }
            emit(["event": "notification", "line": String(decoding: lineData, as: UTF8.self)])
        }
        // 兼容那些 notify 不补分隔符的固件(EspBleLink 会补,所以默认关掉这条)。
        // 靠「看起来像一个完整 JSON 对象」猜边界,本质是猜 —— 只在没得选时才开。
        if config.acceptUnterminated, rxBuffer.count > 1 {
            if let s = String(data: rxBuffer, encoding: .utf8), s.hasSuffix("}") {
                emit(["event": "notification", "line": s])
                rxBuffer.removeAll()
            }
        }
    }

    private func chunkSize() -> Int {
        guard let peripheral else { return 20 }
        let maxLen = peripheral.maximumWriteValueLength(for: .withResponse)
        return max(20, min(config.chunkCeiling, maxLen))
    }

    private func pumpCommands() {
        guard ready, activeSeq == nil else { return }      // 串行:上一条没 ack 完不取新的

        let files: [URL]
        do {
            files = try FileManager.default.contentsOfDirectory(at: config.commandsDir, includingPropertiesForKeys: nil)
                .filter { $0.pathExtension == "json" }
                .sorted { $0.lastPathComponent < $1.lastPathComponent }
        } catch {
            emitError("failed to read command dir: \(error.localizedDescription)")
            return
        }
        guard let next = files.first else { return }
        do {
            let data = try Data(contentsOf: next)
            try FileManager.default.removeItem(at: next)   // 先删再解码,坏文件不会卡死队列
            handleCommand(try JSONDecoder().decode(CommandEnvelope.self, from: data))
        } catch {
            emitError("bad command file \(next.lastPathComponent): \(error.localizedDescription)")
        }
    }

    private func handleCommand(_ envelope: CommandEnvelope) {
        switch envelope.op {
        case "write_json":
            guard let line = envelope.line, let rxChar, let peripheral else {
                emit(["event": "command_error", "seq": envelope.seq, "message": "missing line or RX char"])
                return
            }
            let rawLine = line.hasSuffix("\n") ? line : line + "\n"
            let raw = Data(rawLine.utf8)
            let chunk = chunkSize()
            pendingChunks = stride(from: 0, to: raw.count, by: chunk).map { idx in
                raw.subdata(in: idx..<min(raw.count, idx + chunk))
            }
            activeSeq = envelope.seq
            peripheral.writeValue(pendingChunks.removeFirst(), for: rxChar, type: .withResponse)
        case "ping":
            // 注意:ping 只证明 helper 活着,**探不到 BLE 链路**。
            // 真要探链路死活,上层必须发一次真实的 write_json(见 host 侧 keepalive)。
            emit(["event": "ack", "seq": envelope.seq])
        case "shutdown":
            emit(["event": "ack", "seq": envelope.seq])
            stopping = true
            if let peripheral { central.cancelPeripheralConnection(peripheral) } else { NSApp.terminate(nil) }
        default:
            emit(["event": "command_error", "seq": envelope.seq, "message": "unsupported op: \(envelope.op)"])
        }
    }

    private func sendNextChunk() {
        guard let seq = activeSeq else { return }
        if pendingChunks.isEmpty {
            activeSeq = nil
            emit(["event": "ack", "seq": seq])
            return
        }
        guard let peripheral, let rxChar else {
            activeSeq = nil
            pendingChunks.removeAll()
            emit(["event": "command_error", "seq": seq, "message": "peripheral went away mid-write"])
            return
        }
        peripheral.writeValue(pendingChunks.removeFirst(), for: rxChar, type: .withResponse)
    }

    private func matches(peripheral: CBPeripheral, advertisementData: [String: Any]) -> Bool {
        let identifier = peripheral.identifier.uuidString.uppercased()
        if let expected = config.deviceID?.uppercased() { return expected == identifier }

        let name = (peripheral.name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let localName = (advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)

        if let expectedName = config.deviceName {
            return name == expectedName || localName == expectedName
        }
        if let prefix = config.namePrefix {
            return name.hasPrefix(prefix) || localName.hasPrefix(prefix)
        }
        // 三个匹配条件一个都没给 —— 与其连上随便一台设备,不如什么都不连。
        return false
    }

    private func emit(_ payload: [String: Any]) {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload),
              var line = String(data: data, encoding: .utf8) else { return }
        line.append("\n")
        if let handle = try? FileHandle(forWritingTo: config.eventsURL) {
            _ = try? handle.seekToEnd()
            try? handle.write(contentsOf: Data(line.utf8))
            try? handle.close()
        }
    }

    private func emitError(_ message: String) {
        emit(["event": "error", "message": message])
    }
}

// ============================================================================
// 菜单栏模式(不带 --session-dir 时)
// ============================================================================
//
// 为什么要有这个模式:
//
// 1. **首次蓝牙授权必须有人点一下**,而弹框只在 GUI 会话里出得来。以前双击这个
//    app 什么都不会发生 —— 参数不全,解析失败就退出,压根没碰 CoreBluetooth,
//    系统自然不弹框。人会以为「授权坏了」,其实是根本没请求过。
//
// 2. 平时想知道「链路到底活着没有」,只能去翻 events.jsonl。
//
// ⚠️ 这个模式**不持有业务链路**。真正收发数据的仍然是 Python 拉起来的无界面
//    worker(带 --session-dir 那条路径),它照旧「失败即自我终结、换新进程恢复」——
//    那是绕开 CoreBluetooth 进程级坏状态的唯一手段,不能为了「常驻好看」拆掉。
//    菜单栏这个实例只做两件事:自己扫描看设备在不在,以及**只读**地 tail worker
//    的 events.jsonl 来显示链路状态。

private struct MenuConfig {
    let deviceName: String?
    let namePrefix: String?
    let service: CBUUID
    let sessionDir: URL?

    static func parse() -> MenuConfig {
        var args = Array(CommandLine.arguments.dropFirst())
        func take(_ flag: String) -> String? {
            guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
            let v = args[i + 1]; args.removeSubrange(i...(i + 1))
            return v.isEmpty ? nil : v
        }
        // 运行期参数优先,其次用构建期注入进 Info.plist 的默认值
        let info = Bundle.main.infoDictionary ?? [:]
        func plist(_ k: String) -> String? {
            guard let s = info[k] as? String, !s.isEmpty else { return nil }
            return s
        }
        let name = take("--device-name") ?? plist("ESPBLEDeviceName")
        let prefix = take("--name-prefix") ?? plist("ESPBLENamePrefix")
        let svc = take("--service") ?? plist("ESPBLEService") ?? defaultService
        let sess = take("--watch-session") ?? plist("ESPBLESessionDir")
        return MenuConfig(
            deviceName: name,
            namePrefix: prefix ?? (name == nil ? nil : nil),
            service: CBUUID(string: svc),
            sessionDir: sess.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath,
                                       isDirectory: true) }
        )
    }

    var wanted: String { deviceName ?? namePrefix ?? "(未配置设备名)" }
}

final class MenuBarDelegate: NSObject, NSApplicationDelegate, CBCentralManagerDelegate {
    private var cfg = MenuConfig.parse()
    private var statusItem: NSStatusItem!
    private var central: CBCentralManager!
    private var btState: CBManagerState = .unknown
    private var lastSeen: Date?
    private var lastRSSI = 0
    private var scanStartedAt = Date()
    private var linkConnected = false
    private var linkSince: Date?
    private var lastTelemetry = ""
    private var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        render()
        // 创建 central 这一步才会触发 TCC 弹框 —— 这正是双击 app 该发生的事。
        central = CBCentralManager(delegate: self, queue: nil)
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    // ---- CoreBluetooth ----

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        btState = c.state
        if c.state == .poweredOn { startScan() }
        render()
    }

    private func startScan() {
        guard central?.state == .poweredOn else { return }
        scanStartedAt = Date()
        // 和 worker 一样:全扫后按名字匹配,不按 service 过滤
        // (一张桌子上常有好几台跑 NUS 的设备)
        central.scanForPeripherals(withServices: nil,
                                   options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
    }

    func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let name = (p.name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let local = (advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let match: Bool
        if let want = cfg.deviceName { match = (name == want || local == want) }
        else if let pre = cfg.namePrefix { match = name.hasPrefix(pre) || local.hasPrefix(pre) }
        else { match = false }
        guard match else { return }
        lastSeen = Date()
        lastRSSI = RSSI.intValue
    }

    // ---- 只读地看 worker 的会话 ----

    private func pollSession() {
        guard let dir = cfg.sessionDir else { return }
        let events = dir.appendingPathComponent("events.jsonl")
        guard let h = try? FileHandle(forReadingFrom: events) else {
            linkConnected = false; return
        }
        defer { try? h.close() }
        // 只读尾部 64KB,文件再大也不卡
        let size = (try? h.seekToEnd()) ?? 0
        let start = size > 65536 ? size - 65536 : 0
        try? h.seek(toOffset: start)
        guard let data = try? h.readToEnd(),
              let text = String(data: data, encoding: .utf8) else { return }

        var connected = false
        var since: Date? = linkSince
        for line in text.split(separator: "\n") {
            if line.contains("\"event\":\"connected\"") {
                if !connected { since = Date() }
                connected = true
            } else if line.contains("\"event\":\"disconnected\"") {
                connected = false; since = nil
            } else if line.contains("\"event\":\"notification\"") {
                if let r = line.range(of: "\"line\":\"") {
                    lastTelemetry = String(line[r.upperBound...])
                        .replacingOccurrences(of: "\\\"", with: "\"")
                        .replacingOccurrences(of: "\"}", with: "")
                }
            }
        }
        if connected && !linkConnected { linkSince = Date() } else if !connected { linkSince = nil }
        if connected, linkSince == nil { linkSince = since }
        linkConnected = connected
    }

    private func tick() {
        pollSession()
        render()
    }

    // ---- 菜单 ----

    private func ago(_ d: Date?) -> String {
        guard let d else { return "—" }
        let s = Int(Date().timeIntervalSince(d))
        if s < 60 { return "\(s) 秒前" }
        if s < 3600 { return "\(s / 60) 分钟前" }
        return "\(s / 3600) 小时前"
    }

    private func render() {
        let btOK = btState == .poweredOn
        let seenRecently = lastSeen.map { Date().timeIntervalSince($0) < 20 } ?? false

        // 状态栏标题:一个点表达最重要的那件事
        let dot: String
        let color: NSColor
        if !btOK { dot = "●"; color = .systemRed }
        else if linkConnected { dot = "●"; color = .systemGreen }
        else if seenRecently { dot = "●"; color = .systemYellow }
        else { dot = "○"; color = .secondaryLabelColor }
        statusItem.button?.attributedTitle = NSAttributedString(
            string: dot, attributes: [.foregroundColor: color])

        let menu = NSMenu()
        func row(_ s: String) {
            let i = NSMenuItem(title: s, action: nil, keyEquivalent: "")
            i.isEnabled = false
            menu.addItem(i)
        }

        switch btState {
        case .poweredOn:    row("蓝牙:已授权 ✓")
        case .unauthorized: row("蓝牙:未授权 —— 见下方")
        case .poweredOff:   row("蓝牙:已关闭")
        case .unsupported:  row("蓝牙:不支持")
        default:            row("蓝牙:初始化中…")
        }

        if btState == .unauthorized {
            let i = NSMenuItem(title: "打开 系统设置 > 隐私与安全性 > 蓝牙",
                               action: #selector(openBluetoothPrefs), keyEquivalent: "")
            i.target = self
            menu.addItem(i)
        }

        menu.addItem(.separator())
        if linkConnected {
            // worker 连着时设备不再广播,这里必须说清楚,否则「未发现」会被误读成掉线
            row("链路:已连接 · \(ago(linkSince).replacingOccurrences(of: "前", with: ""))")
            row("设备:被 worker 占用(连接中不广播)")
        } else {
            row("链路:未连接")
            row(seenRecently
                ? "设备:\(cfg.wanted) · \(lastRSSI) dBm · \(ago(lastSeen))"
                : "设备:未发现(已扫 \(Int(Date().timeIntervalSince(scanStartedAt))) 秒)")
        }
        if !lastTelemetry.isEmpty {
            row("遥测:" + String(lastTelemetry.prefix(60)))
        }

        menu.addItem(.separator())
        let rescan = NSMenuItem(title: "重新扫描", action: #selector(rescan), keyEquivalent: "r")
        rescan.target = self
        menu.addItem(rescan)
        if cfg.sessionDir != nil {
            let open = NSMenuItem(title: "打开会话目录", action: #selector(openSession), keyEquivalent: "")
            open.target = self
            menu.addItem(open)
        }
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        statusItem.menu = menu
    }

    @objc private func rescan() {
        central?.stopScan()
        lastSeen = nil
        startScan()
        render()
    }

    @objc private func openSession() {
        guard let d = cfg.sessionDir else { return }
        NSWorkspace.shared.open(d)
    }

    @objc private func openBluetoothPrefs() {
        if let u = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth") {
            NSWorkspace.shared.open(u)
        }
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

@main
struct EspBleHelperMain {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)     // 无 Dock 图标、不抢焦点

        // 带 --session-dir = Python 拉起来的无界面 worker(原有行为,一字未改);
        // 不带 = 用户双击打开的菜单栏模式。
        let isWorker = CommandLine.arguments.contains("--session-dir")
        let delegate: NSApplicationDelegate = isWorker ? AppDelegate() : MenuBarDelegate()
        app.delegate = delegate
        // ⚠️ NSApplication.delegate 是 weak 的,局部变量会被立刻回收 —— 必须留一个强引用
        objc_setAssociatedObject(app, "espble.delegate", delegate, .OBJC_ASSOCIATION_RETAIN)
        app.run()
    }
}
