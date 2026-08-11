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
    // BleHub 模式:注册表列出所有设备,每台的链路状态在 <sessionRoot>/<id>/events.jsonl
    let registryPath: URL?
    let sessionRoot: URL?

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
        let reg  = take("--registry") ?? plist("ESPBLERegistry")
            ?? "~/.config/espble/devices.json"
        let root = take("--session-root") ?? plist("ESPBLESessionRoot")
            ?? "~/.config/espble/sessions"
        func url(_ s: String?) -> URL? {
            guard let s, !s.isEmpty else { return nil }
            return URL(fileURLWithPath: (s as NSString).expandingTildeInPath)
        }
        return MenuConfig(
            deviceName: name,
            namePrefix: prefix,
            service: CBUUID(string: svc),
            sessionDir: sess.map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath,
                                       isDirectory: true) },
            registryPath: url(reg),
            sessionRoot: url(root)
        )
    }

    var wanted: String { deviceName ?? namePrefix ?? "(未配置设备名)" }

    /// 有没有任何可用的匹配条件。没有的话 didDiscover 里 match 恒为 false,
    /// 扫描纯属浪费射频 —— startScan() 靠这个短路掉。
    var hasTarget: Bool { deviceName != nil || namePrefix != nil }
}

/// 注册表里的一台设备 + 它当前的链路状态。
private struct HubDevice {
    let id: String
    let type: String
    let alias: String
    var fw: Int = 0
    var connected = false
    var lastTelemetry = ""
    var name: String { type.isEmpty ? id : "\(type)-\(id)" }
    var label: String { alias.isEmpty ? id : alias }
}

final class MenuBarDelegate: NSObject, NSApplicationDelegate, CBCentralManagerDelegate {
    private var cfg = MenuConfig.parse()
    private var statusItem: NSStatusItem!
    private var central: CBCentralManager!
    private var btState: CBManagerState = .unknown
    private var lastSeen: Date?
    private var lastRSSI = 0
    private var linkConnected = false
    private var linkSince: Date?
    private var lastTelemetry = ""
    private var hubDevices: [HubDevice] = []
    private var timer: Timer?

    // ---- 扫描:能不扫就不扫(见 docs/pitfalls.md C9)----
    //
    // ★ 这里有过两个**方向相反**的错误,两个都真实发生过,别再走回去:
    //
    // 错误一:`AllowDuplicates: true` 且从不 stopScan。一个实例连扫 9 天,
    //   抢射频、拖累同机其它 helper 的链路(C8)。
    //
    // 错误二(修错误一时引入的,更严重):改成「15s 扫 / 45s 停」的占空比。
    //   长驻进程每分钟 start/stopScan 一次,**会把整机的 BLE 扫描投递搞坏** ——
    //   bluetoothd 照常在收广播(system_profiler 里看得到 RSSI),但所有
    //   CBCentralManager 客户端一个 didDiscover 都收不到。实测现象:
    //   菜单栏一开着,collector 就永远连不上设备;把菜单栏进程杀掉,
    //   同一条扫描命令立刻扫到 6 台。**它把自己要显示的那条链路弄断了。**
    //
    // 正解:**菜单栏是个只读的文件显示器,不需要扫描。** 链路状态来自 worker 写的
    // events.jsonl 和注册表 —— 那才是权威。扫描只在「压根没有会话文件可读」的
    // 单设备场景下当兜底,而且是**一次性、不停不启**的连续扫描。
    //
    // (顺带:`AllowDuplicates: false` 下每台设备每次扫描只回调一次,所以靠扫描维持
    //  「最近看到」本来就不可靠 —— 之前那个占空比其实是在拿 churn 换 lastSeen 新鲜度。
    //  既然链路状态有更好的来源,这笔交易压根不该做。)
    private var scanning = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        render()
        // 只在**确实需要扫描**时才创建 CBCentralManager:配好之后(有注册表/会话文件)
        // 菜单栏纯读文件,不碰 CoreBluetooth;只有全新安装(什么都没配)才建 ——
        // 那时既要扫描,也正好需要弹 TCC 授权框(见 B1)。
        //
        // ⚠️ **这一条不解决 C10,别以为它解决了。** 曾经有一版按「持有 central 就会
        // 掐死别人的扫描」这个(错的)成因,把这里从无条件创建改成按需创建,并当成
        // C10 的修复 —— 没用。真成因是 **bundle id**:同一个 id 的两个进程,后起的
        // 那个一个 didDiscover 都收不到,和有没有创建 central 无关(实测菜单栏
        // needsScan 恒为 false、压根没建过 central,扫描照样被掐死)。
        // 所以真正的约束是「一个 bundle id 同时只跑一个进程」——
        // 菜单栏用独立 id(build-helper --install 会改成 <id>.monitor)。详见 C10。
        if needsScan {
            central = CBCentralManager(delegate: self, queue: nil)
        }
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    /// 手动请求蓝牙授权:配好的实例平时不建 central,所以给一个显式入口。
    /// 首次授权的 TCC 弹框只有在 GUI 会话里创建 central 时才会出现。
    @objc private func requestBluetoothAuth() {
        if central == nil {
            central = CBCentralManager(delegate: self, queue: nil)
        }
        render()
    }

    // ---- CoreBluetooth ----

    func centralManagerDidUpdateState(_ c: CBCentralManager) {
        btState = c.state
        if c.state == .poweredOn { startScan() }
        render()
    }

    /// 需要扫描吗?只有「没有别的办法知道设备在不在」时才需要。
    ///
    /// - 有注册表(hub 模式)→ 不需要:每台设备的链路状态在各自的 events.jsonl 里。
    /// - 配了 session-dir(单设备)→ 不需要:同上。
    /// - 没有匹配条件 → 不需要:matches() 恒为 false,扫了也不可能命中
    ///   (这正是「构建时忘了传 --name-prefix」那次的实际后果)。
    private var needsScan: Bool {
        guard cfg.hasTarget else { return false }
        return cfg.registryPath == nil && cfg.sessionDir == nil
    }

    private func startScan() {
        guard central?.state == .poweredOn, needsScan, !scanning else { return }
        scanning = true
        // 和 worker 一样:全扫后按名字匹配,不按 service 过滤
        // (一张桌子上常有好几台跑 NUS 的设备)。
        // AllowDuplicates: false —— 只需要知道「它在」,不需要每个广播包都回调一次。
        // **开一次就一直开着,不要周期性 stop/start**(见上面那段:churn 会搞坏全机扫描)。
        central.scanForPeripherals(withServices: nil,
                                   options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
    }

    private func stopScanIfRunning() {
        guard scanning else { return }
        central?.stopScan()
        scanning = false
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

    /// 读 BleHub 的注册表,并为每台设备读一次它自己的 events.jsonl。
    /// 全程只读 —— 菜单栏实例绝不碰业务链路,那是 worker 进程的事。
    private func pollHub() {
        guard let regURL = cfg.registryPath,
              let data = try? Data(contentsOf: regURL),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let devs = root["devices"] as? [String: [String: Any]] else {
            hubDevices = []
            return
        }
        var out: [HubDevice] = []
        for (id, d) in devs {
            var hd = HubDevice(id: id,
                               type: (d["device_type"] as? String) ?? "",
                               alias: (d["alias"] as? String) ?? "",
                               fw: (d["fw"] as? Int) ?? 0)
            if let sroot = cfg.sessionRoot {
                let ev = sroot.appendingPathComponent(id, isDirectory: true)
                          .appendingPathComponent("events.jsonl")
                let (conn, telem) = readLink(ev)
                hd.connected = conn
                hd.lastTelemetry = telem
            }
            out.append(hd)
        }
        hubDevices = out.sorted { $0.label < $1.label }
    }

    /// 只读地判断一条链路的死活 + 最近一条遥测。
    ///
    /// ⚠️ 这里曾经只读**尾部 32KB**,注释还写着「32KB 足够」。那是错的,而且错得反直觉:
    /// `connected` 事件**整条连接只写一次**(在文件很前面),而连接活着的每一秒都在往后面
    /// 追加 notification / ack。所以**链路越健康、连得越久,connected 越确定地被挤出窗口**
    /// —— 菜单栏就越确定地把一条好链路显示成「未连接」。
    /// 实测:单连接稳定 3 小时 15 分钟(设备侧 c:1 d:0、遥测每 20 秒在落)之后,
    /// 文件里堆了约 120KB,菜单栏显示「0/1 在线 · 未连接」。
    ///
    /// 之所以一直没暴露:以前链路一直在 flapping,worker 每几秒重开一次、
    /// events.jsonl 每次都被截断,`connected` 永远就在尾部。**症状被 bug 藏在另一个 bug 后面。**
    ///
    /// 现在从尾部逐级放大窗口,直到找到一个 connect/disconnect 标记(或读完整个文件)。
    /// 逐级而不是一次读全文:健康的长连接会让这个文件长到几 MB,而菜单栏是 2 秒一轮的。
    private func readLink(_ url: URL) -> (Bool, String) {
        guard let h = try? FileHandle(forReadingFrom: url) else { return (false, "") }
        defer { try? h.close() }
        let size = (try? h.seekToEnd()) ?? 0

        for window in [32_768, 512_000, Int(size)] {
            let from = size > UInt64(window) ? size - UInt64(window) : 0
            try? h.seek(toOffset: from)
            guard let d = try? h.readToEnd(),
                  let text = String(data: d, encoding: .utf8) else { return (false, "") }

            var mark: Bool? = nil        // nil = 这个窗口里压根没有 connect/disconnect
            var telem = ""
            for line in text.split(separator: "\n") {
                if line.contains("\"event\":\"connected\"") { mark = true }
                else if line.contains("\"event\":\"disconnected\"") { mark = false }
                else if line.contains("\"event\":\"notification\"") {
                    if let r = line.range(of: "\"line\":\"") {
                        telem = String(line[r.upperBound...])
                            .replacingOccurrences(of: "\\\"", with: "\"")
                            .replacingOccurrences(of: "\"}", with: "")
                    }
                }
            }
            // 找到标记就用它;没找到而且窗口还没覆盖全文,就放大再来一遍。
            if let m = mark { return (m, telem) }
            if from == 0 { return (false, telem) }   // 全文都没有 → 真的没连过
        }
        return (false, "")
    }

    private func tick() {
        // 只在"确实需要扫"且还没在扫时开一次;需求消失(比如注册表出现了)就停掉。
        // 注意这不是占空比 —— 它由状态变化驱动,一小时最多翻转一两次,不会 churn。
        if needsScan { startScan() } else { stopScanIfRunning() }
        pollHub()
        if hubDevices.isEmpty { pollSession() }   // 没有注册表就退回单设备模式
        render()
    }

    // ---- 菜单 ----

    /// 把设备上报的遥测 JSON 排成紧凑的 `k=v` 一行。
    ///
    /// ⚠️ **刻意不解读语义。** `pct` / `up` / `v` 这些是**应用**自己定的字段
    /// (m5paper-monitor 用它们表示电量/运行秒数/固件版本),框架不该认识它们 ——
    /// 这个仓库一直守着「应用不持有协议细节、框架不持有业务字段」这条线,
    /// 一旦在这里 `if key == "pct" { 显示成百分比 }`,框架就绑死到一个应用上了。
    ///
    /// 代价是拿不到"100% · 已运行 17 分钟"那种人性化排版。要那个得让应用侧传格式提示,
    /// 那是另一件事。当前这样已经比原来 `prefix(52)` 硬截断原始字符串好得多
    /// (截断会把 JSON 切在半个转义序列上,读起来是乱码)。
    static func formatTelemetry(_ raw: String, budget: Int = 56) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        guard let data = trimmed.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              !obj.isEmpty else {
            // 不是 JSON(或是空对象)就退回原样,按**字符**截断以免切坏 UTF-8。
            // 空对象没什么可显示的,直接不显示。
            if trimmed == "{}" || trimmed == "[]" { return nil }
            return String(trimmed.prefix(budget))
        }

        // ⚠️ 按**原始 JSON 里的字段顺序**取,不要按字母序。
        //    字母序 + 截断会把最有用的字段切掉 —— 实测 m5paper 的遥测按字母序只剩
        //    `c chg d g5 ls`,而 pct / up / v 全被挤掉了。应用把重要字段写在前面,尊重它。
        //    (Swift 字典是无序的,所以顺序得从原文里捞;直接遍历字典还会每次刷新都跳。)
        var order: [String] = []
        var picked = Set<String>()
        let ns = trimmed as NSString
        if let re = try? NSRegularExpression(pattern: "\"([^\"]+)\"\\s*:") {
            re.enumerateMatches(in: trimmed, range: NSRange(location: 0, length: ns.length)) { m, _, _ in
                guard let m, m.numberOfRanges > 1 else { return }
                let k = ns.substring(with: m.range(at: 1))
                if obj[k] != nil, picked.insert(k).inserted { order.append(k) }
            }
        }
        if order.isEmpty { order = obj.keys.sorted() }      // 正则没捞到时的兜底

        var parts: [String] = []
        var used = 0
        for k in order {
            // **只渲染标量。** 嵌套对象/数组会被 Swift 的 description 排成多行,
            // 直接把菜单行搞烂(实测 `a={\n b = 1;\n}`)。
            let piece: String
            switch obj[k] {
            case let n as NSNumber: piece = "\(k)=\(n)"
            case let s as String:   piece = "\(k)=\(s.prefix(12))"
            default: continue
            }
            // 按总长度收口而不是按字段个数 —— 一行能塞多少就塞多少,信息量最大化
            if used + piece.count + 1 > budget { break }
            parts.append(piece)
            used += piece.count + 1
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    private func ago(_ d: Date?) -> String {
        guard let d else { return "—" }
        let s = Int(Date().timeIntervalSince(d))
        if s < 60 { return "\(s) 秒前" }
        if s < 3600 { return "\(s / 60) 分钟前" }
        return "\(s / 3600) 小时前"
    }

    /// 菜单栏本体:一个天线图标 + 每台设备一个点(实心=在线,空心=掉线)。
    ///
    /// 三条设计约束,改之前先读:
    /// 1. **图标用 template image。** 菜单栏深浅色会自动反相,写死颜色在浅色栏里看不清。
    ///    状态主要靠**图标形状**(有没有斜杠)和**点的实心/空心**表达,不是靠颜色 ——
    ///    这样色盲也能用,深浅色切换也不会糊。红色只留给"蓝牙压根不能用"。
    /// 2. **`NSImage(systemSymbolName:)` 是 optional**,符号名打错会静默变成
    ///    **空白菜单栏项**(点不到、看不见,极难查)。所以必须 nil 回退到文字字形。
    /// 3. 设备多了点会挤 —— 超过 dotLimit 台就退化成 `3/5` 数字。
    private static let dotLimit = 4

    private func renderStatusItem(btOK: Bool, seenRecently: Bool) {
        guard let button = statusItem.button else { return }

        // ---- 图标 ----
        // 蓝牙不可用时用带斜杠的变体:一眼看出"不是设备的问题,是本机蓝牙的问题"。
        let symbol = btOK ? "antenna.radiowaves.left.and.right"
                          : "antenna.radiowaves.left.and.right.slash"
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: "ESP BLE") {
            img.isTemplate = true          // 跟随菜单栏深浅色
            button.image = img
            button.imagePosition = .imageLeading
        } else {
            // 回退:符号不存在(改错名字/更老的系统)也得让人看见东西
            button.image = nil
        }
        let fellBack = button.image == nil

        // ---- 点 / 计数 ----
        var text = ""
        var color: NSColor = .labelColor
        if !btOK {
            text = fellBack ? " 蓝牙✕" : ""
            color = .systemRed
        } else if !hubDevices.isEmpty {
            let up = hubDevices.filter { $0.connected }.count
            if hubDevices.count > Self.dotLimit {
                text = " \(up)/\(hubDevices.count)"      // 太多了,退化成数字
            } else {
                // 顺序跟下拉菜单里一致(都按 label 排),否则对不上号
                text = " " + hubDevices.map { $0.connected ? "●" : "○" }.joined()
            }
            color = up == 0 ? .secondaryLabelColor : .labelColor
        } else {
            // 单设备模式(没有注册表)。sessionDir 都没配时我们其实不知道链路状态,
            // 别画一个看起来很确定的空心点 —— 用问号表达"不知道"。
            if cfg.sessionDir == nil && !cfg.hasTarget {
                text = " ?"
                color = .secondaryLabelColor
            } else {
                text = linkConnected || seenRecently ? " ●" : " ○"
                color = linkConnected ? .labelColor : .secondaryLabelColor
            }
        }
        if fellBack && btOK && text.isEmpty { text = " BLE" }
        button.attributedTitle = NSAttributedString(
            string: text,
            attributes: [.foregroundColor: color,
                         .font: NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular)])
    }

    private func render() {
        let btOK = btState == .poweredOn
        let seenRecently = lastSeen.map { Date().timeIntervalSince($0) < 20 } ?? false

        renderStatusItem(btOK: btOK, seenRecently: seenRecently)

        let menu = NSMenu()
        func row(_ s: String) {
            let i = NSMenuItem(title: s, action: nil, keyEquivalent: "")
            i.isEnabled = false
            menu.addItem(i)
        }

        if central == nil {
            // 平时不建 central(否则会抢掉 worker 的扫描,见 C10),所以这里**不知道**
            // 蓝牙状态 —— 又是"看不到就说看不到",不要编一个"已授权 ✓"出来。
            // 真出了授权问题,worker 会把 `bluetooth unauthorized` 写进 events.jsonl,
            // 下面的链路行会显示出来。
            row("蓝牙:未查询(本进程不占用蓝牙)")
        } else {
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
        }

        menu.addItem(.separator())
        if !hubDevices.isEmpty {
            // BleHub 模式:注册表里每台设备一行
            let up = hubDevices.filter { $0.connected }.count
            row("设备:\(up)/\(hubDevices.count) 在线")
            for d in hubDevices {
                var line = "  \(d.connected ? "●" : "○")  \(d.label)"
                if d.fw > 0 { line += "   fw\(d.fw)" }
                if !d.connected { line += "   未连接" }
                row(line)
                // ⚠️ 这里**故意不显示 RSSI**。设备连上之后就不再广播了,菜单栏扫不到它,
                //    手上没有任何实时信号强度。显示一个连接时的旧值等于拿陈旧数据冒充实时。
                if d.connected, let t = Self.formatTelemetry(d.lastTelemetry) {
                    row("        " + t)
                }
            }
        } else if linkConnected {
            // worker 连着时设备不再广播,这里必须说清楚,否则「未发现」会被误读成掉线
            row("链路:已连接 · \(ago(linkSince).replacingOccurrences(of: "前", with: ""))")
            row("设备:被 worker 占用(连接中不广播)")
            if let t = Self.formatTelemetry(lastTelemetry) { row("遥测:" + t) }
        } else if cfg.sessionDir == nil {
            // ⚠️ 没配 session-dir → pollSession() 直接 return,我们**根本没法知道**链路状态。
            //    这时候显示「未连接」是撒谎,而且是最坏的那种:worker 可能正连着,
            //    而设备连接期间**不广播**,于是下面还会显示「设备未发现」——
            //    两条错误互相印证,看起来像铁证。实际踩过:链路好着呢,菜单说没连。
            //    看不到就说看不到。
            row("链路:未知 —— 没配 session-dir,看不到 worker")
            row("      重建时加 --session-dir <worker 的会话目录>")
        } else {
            row("链路:未连接")
            // ⚠️ 没配匹配条件时必须明说。以前这里显示「未发现(已扫 80 万秒)」——
            //    读起来像「设备不在」,实际是构建 helper 时漏了 --name-prefix,
            //    它压根没在找任何东西。这条误导过一次,别再让它沉默。
            if !cfg.hasTarget {
                row("设备:⚠️ 未配置匹配条件,不扫描")
                row("      重建时带上 --name-prefix 或 --device-name")
            } else if seenRecently {
                row("设备:\(cfg.wanted) · \(lastRSSI) dBm · \(ago(lastSeen))")
            } else if scanning {
                row("设备:未发现(扫描中)")
            } else {
                // 有会话/注册表可读时我们**故意不扫**,所以这里不能说「未发现」——
                // 那会读成"设备不在",而真相是"我没在找,因为有更好的来源"。
                row("设备:—(不扫描,链路状态以会话文件为准)")
            }
            if let t = Self.formatTelemetry(lastTelemetry) { row("遥测:" + t) }
        }

        menu.addItem(.separator())
        // 只有真的会扫的时候才给这个菜单项 —— 一个点了没反应的按钮比没有更糟。
        if needsScan {
            let rescan = NSMenuItem(title: "重新扫描", action: #selector(rescan), keyEquivalent: "r")
            rescan.target = self
            menu.addItem(rescan)
        } else if central == nil {
            // 平时不占蓝牙,但首次授权必须在 GUI 会话里创建一次 central 才弹得出框(B1)。
            // ⚠️ 点了会临时占用蓝牙,可能干扰 worker 的扫描(C10)—— 标题里说明白。
            let auth = NSMenuItem(title: "请求蓝牙授权(会临时占用蓝牙)",
                                  action: #selector(requestBluetoothAuth), keyEquivalent: "")
            auth.target = self
            menu.addItem(auth)
        }
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
        // 必须走 stopScanIfRunning():它会清掉 scanning 标志,
        // 否则下面 startScan() 的 `!scanning` 守卫会把这次重扫直接吃掉。
        // 手点一次算状态变化驱动,不是周期 churn,可以接受。
        stopScanIfRunning()
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
