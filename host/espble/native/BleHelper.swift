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

@main
struct EspBleHelperMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.setActivationPolicy(.accessory)     // 无 Dock 图标、不抢焦点
        app.delegate = delegate
        app.run()
    }
}
