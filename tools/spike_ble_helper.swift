import CoreBluetooth
import Darwin
import Foundation

private let spikeServiceUUID = CBUUID(string: "0000FD02-0000-1000-8000-00805F9B34FB")
private let spikeRxCharacteristicUUID = CBUUID(string: "0000FD02-0001-1000-8000-00805F9B34FB")
private let spikeTxCharacteristicUUID = CBUUID(string: "0000FD02-0002-1000-8000-00805F9B34FB")

private let highPriorityDelimiter: UInt8 = 0x01
private let messageDelimiter: UInt8 = 0x02
private let escapeXor: UInt8 = 0x03
// LEGO's SPIKE protocol uses a custom COBS variant where the code word
// offset is 2, not 3. This matches the official documentation and example
// implementation, and is what allows bytes 0x00, 0x01, and 0x02 to be
// escaped correctly before the XOR/framing step.
private let cobsCodeOffset: UInt8 = 0x02
private let maxCobsBlockSize = 84

private struct ScanResult: Codable {
    let name: String
    let identifier: String
}

private struct UploadResult: Codable {
    let deviceIdentifier: String
    let hubName: String
    let slot: Int
}

private struct StopResult: Codable {
    let deviceIdentifier: String
    let hubName: String
    let slot: Int
}

private struct SessionReady: Codable {
    let pid: Int32
    let deviceIdentifier: String
    let hubName: String
}

private struct SessionCommand: Codable {
    let id: String
    let command: String
    let programPath: String?
    let slot: Int?
    let autoStopBeforeStart: Bool?
    let stopRunning: Bool?
}

private struct SessionResponse: Codable {
    let id: String
    let ok: Bool
    let deviceIdentifier: String?
    let hubName: String?
    let slot: Int?
    let error: String?
}

private enum HelperError: Error, CustomStringConvertible {
    case usage(String)
    case bluetoothUnavailable(String)
    case timeout(String)
    case notFound(String)
    case protocolError(String)
    case fileError(String)

    var description: String {
        switch self {
        case .usage(let message),
             .bluetoothUnavailable(let message),
             .timeout(let message),
             .notFound(let message),
             .protocolError(let message),
             .fileError(let message):
            return message
        }
    }
}

private enum MessageType: UInt8 {
    case infoRequest = 0x00
    case infoResponse = 0x01
    case getHubNameRequest = 0x18
    case getHubNameResponse = 0x19
    case deviceUUIDRequest = 0x1A
    case deviceUUIDResponse = 0x1B
    case startFileUploadRequest = 0x0C
    case startFileUploadResponse = 0x0D
    case transferChunkRequest = 0x10
    case transferChunkResponse = 0x11
    case programFlowRequest = 0x1E
    case programFlowResponse = 0x1F
    case programFlowNotification = 0x20
    case consoleNotification = 0x21
    case clearSlotRequest = 0x46
    case clearSlotResponse = 0x47
}

private enum ProgramAction: UInt8 {
    case start = 0
    case stop = 1
}

private enum ResponseStatus: UInt8 {
    case ack = 0
    case nack = 1
}

private struct InfoResponse {
    let maxPacketSize: Int
    let maxChunkSize: Int
}

private enum ParsedMessage {
    case info(InfoResponse)
    case status(MessageType, ResponseStatus)
    case hubName(String)
    case deviceUUID(Data)
    case console(String)
    case flow(action: UInt8)
    case unknown(UInt8, Data)
}

private struct Request {
    let command: String
    let outputPath: URL?
    let sessionDir: URL?
    let targetName: String?
    let deviceIdentifier: String?
    let programPath: URL?
    let slot: Int?
    let autoStopBeforeStart: Bool
}

private struct ConnectedHub {
    let maxPacketSize: Int
    let maxChunkSize: Int
    let hubName: String
    let deviceIdentifier: String
}

private final class HubController: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    private var central: CBCentralManager?
    private var peripheral: CBPeripheral?
    private var txCharacteristic: CBCharacteristic?
    private var rxCharacteristic: CBCharacteristic?

    private var powerOnContinuation: CheckedContinuation<Void, Error>?
    private var scanResultsContinuation: CheckedContinuation<[ScanResult], Error>?
    private var scanPeripheralContinuation: CheckedContinuation<CBPeripheral, Error>?
    private var readyContinuation: CheckedContinuation<Void, Error>?
    private var messageContinuation: CheckedContinuation<ParsedMessage, Error>?

    private var powerOnTimeout: DispatchWorkItem?
    private var scanResultsTimeout: DispatchWorkItem?
    private var scanPeripheralTimeout: DispatchWorkItem?
    private var readyTimeout: DispatchWorkItem?
    private var messageTimeout: DispatchWorkItem?

    private var scanTargetName: String?
    private var scanResultsByID: [UUID: ScanResult] = [:]
    private var pendingMessages: [ParsedMessage] = []
    private var lowPriorityBuffer: [UInt8] = []
    private var highPriorityBuffer: [UInt8]?
    private var connectedHub: ConnectedHub?

    func scan(targetName: String?) async throws -> [ScanResult] {
        try await ensurePoweredOn()

        scanResultsByID = [:]
        scanTargetName = nil

        return try await withCheckedThrowingContinuation { continuation in
            scanResultsContinuation = continuation
            central?.scanForPeripherals(withServices: [spikeServiceUUID], options: nil)

            let timeout = DispatchWorkItem { [weak self] in
                guard let self, let continuation = self.scanResultsContinuation else {
                    return
                }
                self.central?.stopScan()
                self.scanResultsContinuation = nil
                let results = self.scanResultsByID.values.sorted { $0.name < $1.name }
                continuation.resume(returning: targetName == nil ? results : results.filter { self.matches(name: $0.name, targetName: targetName!) })
            }

            scanResultsTimeout = timeout
            DispatchQueue.main.asyncAfter(deadline: .now() + 5.0, execute: timeout)
        }
    }

    func upload(request: Request) async throws -> UploadResult {
        guard let programPath = request.programPath else {
            throw HelperError.usage("The upload command needs --program <path>.")
        }
        guard let slot = request.slot, (0 ... 19).contains(slot) else {
            throw HelperError.usage("The upload command needs --slot <0-19>.")
        }

        let programSource: Data
        do {
            programSource = try Data(contentsOf: programPath)
        } catch {
            throw HelperError.fileError("Could not read the generated Hub program at \(programPath.path).")
        }

        let connectedHub = try await ensureHubSession(request: request)

        if request.autoStopBeforeStart {
            _ = try await requestStatus(
                payload: buildProgramFlowRequest(action: .stop, slot: UInt8(slot)),
                expectedType: .programFlowResponse,
                maxPacketSize: connectedHub.maxPacketSize,
                description: "stop the current Hub program",
                allowNack: true
            )
        }

        _ = try await requestStatus(
            payload: buildClearSlotRequest(slot: UInt8(slot)),
            expectedType: .clearSlotResponse,
            maxPacketSize: connectedHub.maxPacketSize,
            description: "clear the target slot"
        )

        // The official protocol example stores the runnable source on the Hub
        // as `program.py`. Matching that file name avoids relying on launcher
        // behavior for arbitrary names such as `hub_program.py`.
        try await uploadProgram(
            programSource: programSource,
            filename: "program.py",
            slot: UInt8(slot),
            info: InfoResponse(maxPacketSize: connectedHub.maxPacketSize, maxChunkSize: connectedHub.maxChunkSize)
        )

        _ = try await requestStatus(
            payload: buildProgramFlowRequest(action: .start, slot: UInt8(slot)),
            expectedType: .programFlowResponse,
            maxPacketSize: connectedHub.maxPacketSize,
            description: "start the uploaded program"
        )
        try await observeProgramStartup()

        return UploadResult(
            deviceIdentifier: connectedHub.deviceIdentifier,
            hubName: connectedHub.hubName,
            slot: slot
        )
    }

    func stop(request: Request) async throws -> StopResult {
        guard let slot = request.slot, (0 ... 19).contains(slot) else {
            throw HelperError.usage("The stop command needs --slot <0-19>.")
        }

        let connectedHub = try await ensureHubSession(request: request)

        _ = try await requestStatus(
            payload: buildProgramFlowRequest(action: .stop, slot: UInt8(slot)),
            expectedType: .programFlowResponse,
            maxPacketSize: connectedHub.maxPacketSize,
            description: "stop the current Hub program",
            allowNack: true
        )

        return StopResult(
            deviceIdentifier: connectedHub.deviceIdentifier,
            hubName: connectedHub.hubName,
            slot: slot
        )
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn:
            cancelPowerOnTimeout()
            powerOnContinuation?.resume()
            powerOnContinuation = nil
        case .unsupported:
            failAllPending(with: HelperError.bluetoothUnavailable("Bluetooth is unsupported on this Mac."))
        case .unauthorized:
            failAllPending(with: HelperError.bluetoothUnavailable("Bluetooth access was denied by macOS."))
        case .poweredOff:
            failAllPending(with: HelperError.bluetoothUnavailable("Bluetooth is turned off."))
        case .resetting, .unknown:
            break
        @unknown default:
            failAllPending(with: HelperError.bluetoothUnavailable("Bluetooth entered an unknown state."))
        }
    }

    func centralManager(
        _: CBCentralManager,
        didDiscover peripheral: CBPeripheral,
        advertisementData _: [String: Any],
        rssi _: NSNumber
    ) {
        let name = peripheral.name?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let name, !name.isEmpty else {
            return
        }

        scanResultsByID[peripheral.identifier] = ScanResult(name: name, identifier: peripheral.identifier.uuidString)

        guard let targetName = scanTargetName, matches(name: name, targetName: targetName) else {
            return
        }

        cancelScanPeripheralTimeout()
        central?.stopScan()
        scanPeripheralContinuation?.resume(returning: peripheral)
        scanPeripheralContinuation = nil
    }

    func centralManager(_: CBCentralManager, didConnect peripheral: CBPeripheral) {
        self.peripheral = peripheral
        peripheral.delegate = self
        peripheral.discoverServices(nil)
    }

    func centralManager(_: CBCentralManager, didFailToConnect _: CBPeripheral, error: Error?) {
        cancelReadyTimeout()
        let message = error?.localizedDescription ?? "The Hub could not be reached over Bluetooth."
        readyContinuation?.resume(throwing: HelperError.bluetoothUnavailable(message))
        readyContinuation = nil
    }

    func centralManager(_: CBCentralManager, didDisconnectPeripheral _: CBPeripheral, error: Error?) {
        let message = error?.localizedDescription ?? "The Hub disconnected unexpectedly."
        invalidateHubSession()

        cancelReadyTimeout()
        readyContinuation?.resume(throwing: HelperError.bluetoothUnavailable(message))
        readyContinuation = nil

        cancelMessageTimeout()
        messageContinuation?.resume(throwing: HelperError.protocolError(message))
        messageContinuation = nil
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error {
            finishReadyWithError(error.localizedDescription)
            return
        }

        guard let services = peripheral.services else {
            finishReadyWithError("The Hub did not report any Bluetooth services.")
            return
        }

        guard let service = services.first(where: { $0.uuid == spikeServiceUUID }) else {
            let available = services.map { $0.uuid.uuidString }.sorted().joined(separator: ", ")
            let suffix = available.isEmpty ? "" : " Available services: \(available)."
            finishReadyWithError("The SPIKE RPC service was not found on the Hub.\(suffix)")
            return
        }

        peripheral.discoverCharacteristics([spikeTxCharacteristicUUID, spikeRxCharacteristicUUID], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error {
            finishReadyWithError(error.localizedDescription)
            return
        }

        txCharacteristic = service.characteristics?.first(where: { $0.uuid == spikeTxCharacteristicUUID })
        rxCharacteristic = service.characteristics?.first(where: { $0.uuid == spikeRxCharacteristicUUID })

        guard let txCharacteristic, rxCharacteristic != nil else {
            finishReadyWithError("The SPIKE RPC characteristics were not found on the Hub.")
            return
        }

        peripheral.setNotifyValue(true, for: txCharacteristic)
    }

    func peripheral(_: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            finishReadyWithError(error.localizedDescription)
            return
        }
        if characteristic.uuid == spikeTxCharacteristicUUID, characteristic.isNotifying {
            cancelReadyTimeout()
            readyContinuation?.resume()
            readyContinuation = nil
        }
    }

    func peripheral(_: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            finishMessageWithError(error.localizedDescription)
            return
        }
        guard characteristic.uuid == spikeTxCharacteristicUUID, let value = characteristic.value else {
            return
        }
        appendNotificationBytes(Array(value))
    }

    private func ensurePoweredOn() async throws {
        if central == nil {
            central = CBCentralManager(delegate: self, queue: nil)
        }
        guard let central else {
            throw HelperError.bluetoothUnavailable("Bluetooth could not be initialized.")
        }
        if central.state == .poweredOn {
            return
        }

        try await withCheckedThrowingContinuation { continuation in
            powerOnContinuation = continuation
            schedulePowerOnTimeout()
        }
    }

    private func resolvePeripheral(deviceIdentifier: String?, targetName: String?) async throws -> CBPeripheral {
        try await ensurePoweredOn()

        // A cached CoreBluetooth UUID is useful, but on macOS it can point to a
        // stale peripheral object after a previous upload/run cycle. Scanning by
        // the user-visible Hub name is slower, but much more reliable, so we
        // prefer that path and only fall back to the cached UUID.
        if let targetName, !targetName.isEmpty {
            do {
                return try await scanForPeripheral(named: targetName)
            } catch {
                if let deviceIdentifier,
                   let uuid = UUID(uuidString: deviceIdentifier),
                   let cachedPeripheral = central?.retrievePeripherals(withIdentifiers: [uuid]).first
                {
                    return cachedPeripheral
                }
                throw error
            }
        }

        if let deviceIdentifier,
           let uuid = UUID(uuidString: deviceIdentifier),
           let cachedPeripheral = central?.retrievePeripherals(withIdentifiers: [uuid]).first
        {
            return cachedPeripheral
        }

        throw HelperError.notFound("No cached device UUID was available and no Hub name was provided.")
    }

    private func scanForPeripheral(named targetName: String) async throws -> CBPeripheral {
        scanResultsByID = [:]
        scanTargetName = targetName

        return try await withCheckedThrowingContinuation { continuation in
            scanPeripheralContinuation = continuation
            central?.scanForPeripherals(withServices: [spikeServiceUUID], options: nil)
            scheduleScanPeripheralTimeout(targetName: targetName)
        }
    }

    private func connectAndPrepare(to peripheral: CBPeripheral) async throws {
        try await withCheckedThrowingContinuation { continuation in
            readyContinuation = continuation
            central?.connect(peripheral, options: nil)
            scheduleReadyTimeout()
        }
    }

    func ensureHubSession(request: Request) async throws -> ConnectedHub {
        if let connectedHub,
           let peripheral,
           peripheral.state == .connected,
           txCharacteristic != nil,
           rxCharacteristic != nil
        {
            return connectedHub
        }

        let hubPeripheral = try await resolvePeripheral(deviceIdentifier: request.deviceIdentifier, targetName: request.targetName)
        try await connectAndPrepare(to: hubPeripheral)

        let info = try await requestInfo()
        let hubName = try await requestHubName(maxPacketSize: info.maxPacketSize)
        _ = try await requestDeviceUUID(maxPacketSize: info.maxPacketSize)

        let connectedHub = ConnectedHub(
            maxPacketSize: info.maxPacketSize,
            maxChunkSize: info.maxChunkSize,
            hubName: hubName.isEmpty ? (hubPeripheral.name ?? "") : hubName,
            deviceIdentifier: hubPeripheral.identifier.uuidString
        )
        self.connectedHub = connectedHub
        return connectedHub
    }

    func disconnectFromHub() {
        if let peripheral {
            central?.cancelPeripheralConnection(peripheral)
        }
        invalidateHubSession()
    }

    private func invalidateHubSession() {
        connectedHub = nil
        peripheral = nil
        txCharacteristic = nil
        rxCharacteristic = nil
        pendingMessages.removeAll(keepingCapacity: false)
        lowPriorityBuffer.removeAll(keepingCapacity: false)
        highPriorityBuffer = nil
    }

    private func requestInfo() async throws -> InfoResponse {
        try sendMessage(buildInfoRequest(), maxPacketSize: nil)

        while true {
            let message = try await nextMessage(timeoutLabel: "wait for the Hub info response")
            switch message {
            case .info(let info):
                return info
            case .console(let text):
                if !text.isEmpty { print("Hub: \(text)") }
            case .flow(let action):
                print("Hub flow update: action=\(action)")
            default:
                throw HelperError.protocolError("The Hub returned an unexpected response while requesting Info.")
            }
        }
    }

    private func requestHubName(maxPacketSize: Int) async throws -> String {
        try sendMessage(buildHubNameRequest(), maxPacketSize: maxPacketSize)

        while true {
            let message = try await nextMessage(timeoutLabel: "wait for the Hub name")
            switch message {
            case .hubName(let name):
                return name
            case .console(let text):
                if !text.isEmpty { print("Hub: \(text)") }
            case .flow(let action):
                print("Hub flow update: action=\(action)")
            default:
                throw HelperError.protocolError("The Hub returned an unexpected response while requesting its name.")
            }
        }
    }

    private func requestDeviceUUID(maxPacketSize: Int) async throws -> Data {
        try sendMessage(buildDeviceUUIDRequest(), maxPacketSize: maxPacketSize)

        while true {
            let message = try await nextMessage(timeoutLabel: "wait for the Hub device UUID")
            switch message {
            case .deviceUUID(let data):
                return data
            case .console(let text):
                if !text.isEmpty { print("Hub: \(text)") }
            case .flow(let action):
                print("Hub flow update: action=\(action)")
            default:
                throw HelperError.protocolError("The Hub returned an unexpected response while requesting its device UUID.")
            }
        }
    }

    private func requestStatus(
        payload: Data,
        expectedType: MessageType,
        maxPacketSize: Int,
        description: String,
        allowNack: Bool = false
    ) async throws -> ResponseStatus {
        try sendMessage(payload, maxPacketSize: maxPacketSize)

        while true {
            let message = try await nextMessage(timeoutLabel: description)
            switch message {
            case .status(let messageType, let status):
                guard messageType == expectedType else {
                    throw HelperError.protocolError("The Hub responded to \(description) with the wrong message type.")
                }
                if status == .ack || allowNack {
                    return status
                }
                throw HelperError.protocolError("The Hub rejected the request to \(description).")
            case .console(let text):
                if !text.isEmpty { print("Hub: \(text)") }
            case .flow(let action):
                print("Hub flow update: action=\(action)")
            default:
                throw HelperError.protocolError("The Hub returned an unexpected response while trying to \(description).")
            }
        }
    }

    private func observeProgramStartup() async throws {
        let deadline = Date().addingTimeInterval(3.0)
        var consoleMessages: [String] = []

        while deadline.timeIntervalSinceNow > 0 {
            let remaining = min(deadline.timeIntervalSinceNow, 0.5)
            do {
                let message = try await nextMessage(
                    timeoutLabel: "observe program startup",
                    timeoutSeconds: max(remaining, 0.1)
                )
                switch message {
                case .console(let text):
                    if !text.isEmpty {
                        consoleMessages.append(text)
                        print("Hub: \(text)")
                    }
                case .flow(let action):
                    print("Hub flow update: action=\(action)")
                    if action == ProgramAction.stop.rawValue {
                        let consoleSuffix = consoleMessages.isEmpty
                            ? ""
                            : " Console output: \(consoleMessages.joined(separator: " | "))"
                        throw HelperError.protocolError(
                            "The Hub stopped the uploaded program immediately after start.\(consoleSuffix)"
                        )
                    }
                default:
                    continue
                }
            } catch let error as HelperError {
                switch error {
                case .timeout:
                    return
                default:
                    throw error
                }
            }
        }
    }

    private func uploadProgram(programSource: Data, filename: String, slot: UInt8, info: InfoResponse) async throws {
        _ = try await requestStatus(
            payload: buildStartFileUploadRequest(filename: filename, slot: slot, fileCRC: crc32ForFile(programSource)),
            expectedType: .startFileUploadResponse,
            maxPacketSize: info.maxPacketSize,
            description: "start the file upload"
        )

        var runningCRC: UInt32 = 0
        let chunkSize = max(info.maxChunkSize, 1)
        var start = 0

        while start < programSource.count {
            let end = min(start + chunkSize, programSource.count)
            let chunk = programSource.subdata(in: start ..< end)
            runningCRC = crc32UpdatePaddedChunk(chunk, seed: runningCRC)

            _ = try await requestStatus(
                payload: buildTransferChunkRequest(chunk: chunk, runningCRC: runningCRC),
                expectedType: .transferChunkResponse,
                maxPacketSize: info.maxPacketSize,
                description: "transfer the next program chunk"
            )

            start = end
        }
    }

    private func sendMessage(_ payload: Data, maxPacketSize: Int?) throws {
        guard let peripheral, let rxCharacteristic else {
            throw HelperError.protocolError("The Hub connection is not ready for writing.")
        }

        let packed = packFrame(payload)
        let packets: [Data]
        if let maxPacketSize, packed.count > maxPacketSize {
            packets = stride(from: 0, to: packed.count, by: maxPacketSize).map { offset in
                packed.subdata(in: offset ..< min(offset + maxPacketSize, packed.count))
            }
        } else {
            packets = [packed]
        }

        for packet in packets {
            peripheral.writeValue(packet, for: rxCharacteristic, type: .withoutResponse)
        }
    }

    private func nextMessage(timeoutLabel: String, timeoutSeconds: Double = 10.0) async throws -> ParsedMessage {
        if !pendingMessages.isEmpty {
            return pendingMessages.removeFirst()
        }

        return try await withCheckedThrowingContinuation { continuation in
            messageContinuation = continuation
            scheduleMessageTimeout(label: timeoutLabel, timeoutSeconds: timeoutSeconds)
        }
    }

    private func appendNotificationBytes(_ bytes: [UInt8]) {
        for byte in bytes {
            if highPriorityBuffer != nil {
                highPriorityBuffer?.append(byte)
                if byte == messageDelimiter, let highPriorityBuffer {
                    if let payload = try? unpackFrame(highPriorityBuffer) {
                        enqueue(parseMessage(payload))
                    }
                    self.highPriorityBuffer = nil
                }
                continue
            }

            if byte == highPriorityDelimiter {
                highPriorityBuffer = [byte]
                continue
            }

            lowPriorityBuffer.append(byte)
            if byte == messageDelimiter {
                if let payload = try? unpackFrame(lowPriorityBuffer) {
                    enqueue(parseMessage(payload))
                }
                lowPriorityBuffer.removeAll(keepingCapacity: true)
            }
        }
    }

    private func enqueue(_ message: ParsedMessage) {
        if let continuation = messageContinuation {
            cancelMessageTimeout()
            messageContinuation = nil
            continuation.resume(returning: message)
        } else {
            pendingMessages.append(message)
        }
    }

    private func matches(name: String, targetName: String) -> Bool {
        name == targetName || name.lowercased() == targetName.lowercased()
    }

    private func finishReadyWithError(_ message: String) {
        cancelReadyTimeout()
        readyContinuation?.resume(throwing: HelperError.bluetoothUnavailable(message))
        readyContinuation = nil
    }

    private func finishMessageWithError(_ message: String) {
        cancelMessageTimeout()
        messageContinuation?.resume(throwing: HelperError.protocolError(message))
        messageContinuation = nil
    }

    private func failAllPending(with error: Error) {
        cancelPowerOnTimeout()
        powerOnContinuation?.resume(throwing: error)
        powerOnContinuation = nil

        cancelScanResultsTimeout()
        scanResultsContinuation?.resume(throwing: error)
        scanResultsContinuation = nil

        cancelScanPeripheralTimeout()
        scanPeripheralContinuation?.resume(throwing: error)
        scanPeripheralContinuation = nil

        finishReadyWithError(String(describing: error))
        finishMessageWithError(String(describing: error))
    }

    private func schedulePowerOnTimeout() {
        cancelPowerOnTimeout()
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, let continuation = self.powerOnContinuation else { return }
            self.powerOnContinuation = nil
            let stateDescription = self.central.map(describeBluetoothState) ?? "uninitialized"
            continuation.resume(
                throwing: HelperError.timeout(
                    "Timed out while waiting for Bluetooth to power on. Current CoreBluetooth state: \(stateDescription)."
                )
            )
        }
        powerOnTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 15.0, execute: timeout)
    }

    private func cancelPowerOnTimeout() {
        powerOnTimeout?.cancel()
        powerOnTimeout = nil
    }

    private func scheduleScanPeripheralTimeout(targetName: String) {
        cancelScanPeripheralTimeout()
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, let continuation = self.scanPeripheralContinuation else { return }
            self.central?.stopScan()
            self.scanPeripheralContinuation = nil
            let visibleNames = self.scanResultsByID.values.map(\.name).sorted().joined(separator: ", ")
            let suffix = visibleNames.isEmpty ? "" : " Visible devices: \(visibleNames)."
            continuation.resume(throwing: HelperError.notFound("Timed out while looking for \(targetName).\(suffix)"))
        }
        scanPeripheralTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 8.0, execute: timeout)
    }

    private func cancelScanPeripheralTimeout() {
        scanPeripheralTimeout?.cancel()
        scanPeripheralTimeout = nil
    }

    private func cancelScanResultsTimeout() {
        scanResultsTimeout?.cancel()
        scanResultsTimeout = nil
    }

    private func scheduleReadyTimeout() {
        cancelReadyTimeout()
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, let continuation = self.readyContinuation else { return }
            self.readyContinuation = nil
            continuation.resume(throwing: HelperError.timeout("Timed out while preparing the Hub connection."))
        }
        readyTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + 20.0, execute: timeout)
    }

    private func cancelReadyTimeout() {
        readyTimeout?.cancel()
        readyTimeout = nil
    }

    private func scheduleMessageTimeout(label: String, timeoutSeconds: Double) {
        cancelMessageTimeout()
        let timeout = DispatchWorkItem { [weak self] in
            guard let self, let continuation = self.messageContinuation else { return }
            self.messageContinuation = nil
            continuation.resume(throwing: HelperError.timeout("Timed out while trying to \(label)."))
        }
        messageTimeout = timeout
        DispatchQueue.main.asyncAfter(deadline: .now() + timeoutSeconds, execute: timeout)
    }

    private func cancelMessageTimeout() {
        messageTimeout?.cancel()
        messageTimeout = nil
    }
}

private func parse(arguments: [String]) throws -> Request {
    guard arguments.count >= 2 else {
        throw HelperError.usage("Usage: spike_ble_helper <scan|upload> [options]")
    }

    var outputPath: URL?
    var sessionDir: URL?
    var targetName: String?
    var deviceIdentifier: String?
    var programPath: URL?
    var slot: Int?
    var autoStopBeforeStart = true

    var index = 2
    while index < arguments.count {
        switch arguments[index] {
        case "--output":
            index += 1
            guard index < arguments.count else { throw HelperError.usage("Missing value after --output.") }
            outputPath = URL(fileURLWithPath: arguments[index])
        case "--session-dir":
            index += 1
            guard index < arguments.count else { throw HelperError.usage("Missing value after --session-dir.") }
            sessionDir = URL(fileURLWithPath: arguments[index], isDirectory: true)
        case "--target-name":
            index += 1
            guard index < arguments.count else { throw HelperError.usage("Missing value after --target-name.") }
            targetName = arguments[index]
        case "--device-identifier":
            index += 1
            guard index < arguments.count else { throw HelperError.usage("Missing value after --device-identifier.") }
            deviceIdentifier = arguments[index]
        case "--program":
            index += 1
            guard index < arguments.count else { throw HelperError.usage("Missing value after --program.") }
            programPath = URL(fileURLWithPath: arguments[index])
        case "--slot":
            index += 1
            guard index < arguments.count, let parsedSlot = Int(arguments[index]) else {
                throw HelperError.usage("Missing or invalid value after --slot.")
            }
            slot = parsedSlot
        case "--no-auto-stop":
            autoStopBeforeStart = false
        default:
            throw HelperError.usage("Unknown option: \(arguments[index])")
        }
        index += 1
    }

    return Request(
        command: arguments[1],
        outputPath: outputPath,
        sessionDir: sessionDir,
        targetName: targetName,
        deviceIdentifier: deviceIdentifier,
        programPath: programPath,
        slot: slot,
        autoStopBeforeStart: autoStopBeforeStart
    )
}

private func encodeOutput<T: Encodable>(_ value: T) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    return try encoder.encode(value)
}

private func writeOutput(_ data: Data, to outputPath: URL?) throws {
    if let outputPath {
        try data.write(to: outputPath)
    } else {
        FileHandle.standardOutput.write(data)
    }
}

private func writeError(_ error: Error, to outputPath: URL?) {
    let payload = ["error": String(describing: error)]
    let data = try! JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
    if let outputPath {
        try? data.write(to: outputPath)
    } else {
        FileHandle.standardError.write(data)
        FileHandle.standardError.write(Data("\n".utf8))
    }
}

private func runSession(request: Request, controller: HubController) async throws {
    guard let sessionDir = request.sessionDir else {
        throw HelperError.usage("The session command needs --session-dir <path>.")
    }
    guard let readyPath = request.outputPath else {
        throw HelperError.usage("The session command needs --output <path> for the ready file.")
    }

    let fileManager = FileManager.default
    let commandsDir = sessionDir.appendingPathComponent("commands", isDirectory: true)
    let responsesDir = sessionDir.appendingPathComponent("responses", isDirectory: true)

    try fileManager.createDirectory(at: sessionDir, withIntermediateDirectories: true)
    try fileManager.createDirectory(at: commandsDir, withIntermediateDirectories: true)
    try fileManager.createDirectory(at: responsesDir, withIntermediateDirectories: true)

    let connectedHub = try await controller.ensureHubSession(request: request)
    let ready = SessionReady(
        pid: getpid(),
        deviceIdentifier: connectedHub.deviceIdentifier,
        hubName: connectedHub.hubName
    )
    try writeOutput(try encodeOutput(ready), to: readyPath)

    while true {
        if let commandURL = try nextSessionCommand(in: commandsDir) {
            let command = try decodeSessionCommand(from: commandURL)
            let response: SessionResponse
            do {
                response = try await handleSessionCommand(
                    command,
                    request: request,
                    controller: controller
                )
            } catch {
                response = SessionResponse(
                    id: command.id,
                    ok: false,
                    deviceIdentifier: nil,
                    hubName: nil,
                    slot: command.slot,
                    error: String(describing: error)
                )
            }
            try writeSessionResponse(response, in: responsesDir)
            try? fileManager.removeItem(at: commandURL)

            if command.command == "shutdown" {
                return
            }
            continue
        }

        try await Task.sleep(nanoseconds: 200_000_000)
    }
}

private func nextSessionCommand(in commandsDir: URL) throws -> URL? {
    let fileManager = FileManager.default
    let urls = try fileManager.contentsOfDirectory(
        at: commandsDir,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    )
    return urls
        .filter { $0.pathExtension == "json" }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
        .first
}

private func decodeSessionCommand(from url: URL) throws -> SessionCommand {
    let data = try Data(contentsOf: url)
    return try JSONDecoder().decode(SessionCommand.self, from: data)
}

private func writeSessionResponse(_ response: SessionResponse, in responsesDir: URL) throws {
    let url = responsesDir.appendingPathComponent("response-\(response.id).json")
    try writeOutput(try encodeOutput(response), to: url)
}

private func handleSessionCommand(
    _ command: SessionCommand,
    request: Request,
    controller: HubController
) async throws -> SessionResponse {
    switch command.command {
    case "ping":
        let connectedHub = try await controller.ensureHubSession(request: request)
        return SessionResponse(
            id: command.id,
            ok: true,
            deviceIdentifier: connectedHub.deviceIdentifier,
            hubName: connectedHub.hubName,
            slot: request.slot,
            error: nil
        )
    case "upload_and_start":
        guard let programPath = command.programPath else {
            return SessionResponse(
                id: command.id,
                ok: false,
                deviceIdentifier: nil,
                hubName: nil,
                slot: nil,
                error: "The upload_and_start session command needs a programPath."
            )
        }
        let uploadRequest = Request(
            command: "upload",
            outputPath: nil,
            sessionDir: request.sessionDir,
            targetName: request.targetName,
            deviceIdentifier: request.deviceIdentifier,
            programPath: URL(fileURLWithPath: programPath),
            slot: command.slot ?? request.slot,
            autoStopBeforeStart: command.autoStopBeforeStart ?? request.autoStopBeforeStart
        )
        let result = try await controller.upload(request: uploadRequest)
        return SessionResponse(
            id: command.id,
            ok: true,
            deviceIdentifier: result.deviceIdentifier,
            hubName: result.hubName,
            slot: result.slot,
            error: nil
        )
    case "stop":
        let stopRequest = Request(
            command: "stop",
            outputPath: nil,
            sessionDir: request.sessionDir,
            targetName: request.targetName,
            deviceIdentifier: request.deviceIdentifier,
            programPath: nil,
            slot: command.slot ?? request.slot,
            autoStopBeforeStart: request.autoStopBeforeStart
        )
        let result = try await controller.stop(request: stopRequest)
        return SessionResponse(
            id: command.id,
            ok: true,
            deviceIdentifier: result.deviceIdentifier,
            hubName: result.hubName,
            slot: result.slot,
            error: nil
        )
    case "shutdown":
        if command.stopRunning ?? true {
            let stopRequest = Request(
                command: "stop",
                outputPath: nil,
                sessionDir: request.sessionDir,
                targetName: request.targetName,
                deviceIdentifier: request.deviceIdentifier,
                programPath: nil,
                slot: command.slot ?? request.slot,
                autoStopBeforeStart: request.autoStopBeforeStart
            )
            _ = try? await controller.stop(request: stopRequest)
        }
        controller.disconnectFromHub()
        return SessionResponse(
            id: command.id,
            ok: true,
            deviceIdentifier: nil,
            hubName: nil,
            slot: command.slot ?? request.slot,
            error: nil
        )
    default:
        return SessionResponse(
            id: command.id,
            ok: false,
            deviceIdentifier: nil,
            hubName: nil,
            slot: nil,
            error: "Unknown session command: \(command.command)"
        )
    }
}

private func buildInfoRequest() -> Data {
    Data([MessageType.infoRequest.rawValue])
}

private func buildHubNameRequest() -> Data {
    Data([MessageType.getHubNameRequest.rawValue])
}

private func buildDeviceUUIDRequest() -> Data {
    Data([MessageType.deviceUUIDRequest.rawValue])
}

private func buildClearSlotRequest(slot: UInt8) -> Data {
    Data([MessageType.clearSlotRequest.rawValue, slot])
}

private func buildProgramFlowRequest(action: ProgramAction, slot: UInt8) -> Data {
    Data([MessageType.programFlowRequest.rawValue, action.rawValue, slot])
}

private func buildStartFileUploadRequest(filename: String, slot: UInt8, fileCRC: UInt32) -> Data {
    let nameBytes = Array(filename.utf8.prefix(31))
    var payload = Data([MessageType.startFileUploadRequest.rawValue])
    payload.append(contentsOf: nameBytes)
    payload.append(Data(repeating: 0, count: 32 - nameBytes.count))
    payload.append(slot)
    payload.append(contentsOf: withUnsafeBytes(of: fileCRC.littleEndian, Array.init))
    return payload
}

private func buildTransferChunkRequest(chunk: Data, runningCRC: UInt32) -> Data {
    var payload = Data([MessageType.transferChunkRequest.rawValue])
    payload.append(contentsOf: withUnsafeBytes(of: runningCRC.littleEndian, Array.init))
    let size = UInt16(chunk.count)
    payload.append(contentsOf: withUnsafeBytes(of: size.littleEndian, Array.init))
    payload.append(chunk)
    return payload
}

private func parseMessage(_ payload: Data) -> ParsedMessage {
    guard let rawType = payload.first else {
        return .unknown(0, payload)
    }

    switch MessageType(rawValue: rawType) {
    case .infoResponse:
        // The official InfoResponse includes 17 bytes:
        // type + RPC version/build + firmware version/build +
        // max packet + max message + max chunk + product/device id.
        guard payload.count >= 17 else { return .unknown(rawType, payload) }
        guard let maxPacketSize = readUInt16LE(payload, at: 9),
              let maxChunkSize = readUInt16LE(payload, at: 13)
        else {
            return .unknown(rawType, payload)
        }
        return .info(InfoResponse(maxPacketSize: maxPacketSize, maxChunkSize: maxChunkSize))
    case .startFileUploadResponse, .transferChunkResponse, .programFlowResponse, .clearSlotResponse:
        guard payload.count >= 2,
              let messageType = MessageType(rawValue: rawType),
              let status = ResponseStatus(rawValue: payload[1])
        else {
            return .unknown(rawType, payload)
        }
        return .status(messageType, status)
    case .getHubNameResponse:
        return .hubName(decodeCString(payload.dropFirst()))
    case .deviceUUIDResponse:
        return .deviceUUID(Data(payload.dropFirst()))
    case .consoleNotification:
        return .console(decodeCString(payload.dropFirst()))
    case .programFlowNotification:
        guard payload.count >= 2 else { return .unknown(rawType, payload) }
        return .flow(action: payload[1])
    default:
        return .unknown(rawType, payload)
    }
}

private func readUInt16LE(_ data: Data, at offset: Int) -> Int? {
    guard offset >= 0, offset + 1 < data.count else {
        return nil
    }
    let low = Int(data[offset])
    let high = Int(data[offset + 1]) << 8
    return low | high
}

private func decodeCString<S: Sequence>(_ bytes: S) -> String where S.Element == UInt8 {
    let data = Data(bytes.prefix { $0 != 0 })
    return String(data: data, encoding: .utf8) ?? ""
}

private func describeBluetoothState(_ central: CBCentralManager) -> String {
    switch central.state {
    case .unknown:
        return "unknown"
    case .resetting:
        return "resetting"
    case .unsupported:
        return "unsupported"
    case .unauthorized:
        return "unauthorized"
    case .poweredOff:
        return "poweredOff"
    case .poweredOn:
        return "poweredOn"
    @unknown default:
        return "unknown(\(central.state.rawValue))"
    }
}

private func packFrame(_ payload: Data) -> Data {
    let encoded = cobsEncode(Array(payload)).map { $0 ^ escapeXor }
    return Data(encoded + [messageDelimiter])
}

private func unpackFrame(_ frame: [UInt8]) throws -> Data {
    guard frame.last == messageDelimiter else {
        throw HelperError.protocolError("The Hub returned a frame without the SPIKE delimiter.")
    }

    let body: [UInt8]
    if frame.first == highPriorityDelimiter {
        body = Array(frame.dropFirst().dropLast())
    } else {
        body = Array(frame.dropLast())
    }

    let decoded = try cobsDecode(body.map { $0 ^ escapeXor })
    return Data(decoded)
}

private func cobsEncode(_ bytes: [UInt8]) -> [UInt8] {
    var buffer = [UInt8]()
    var codeIndex = 0
    var block = 0

    func beginBlock() {
        codeIndex = buffer.count
        buffer.append(0xFF)
        block = 1
    }

    beginBlock()

    for byte in bytes {
        if byte > messageDelimiter {
            buffer.append(byte)
            block += 1
        }

        if byte <= messageDelimiter || block > maxCobsBlockSize {
            if byte <= messageDelimiter {
                let delimiterBase = Int(byte) * maxCobsBlockSize
                let blockOffset = block + Int(cobsCodeOffset)
                buffer[codeIndex] = UInt8(delimiterBase + blockOffset)
            }
            beginBlock()
        }
    }

    buffer[codeIndex] = UInt8(block + Int(cobsCodeOffset))
    return buffer
}

private func cobsDecode(_ bytes: [UInt8]) throws -> [UInt8] {
    guard let first = bytes.first else {
        return []
    }

    func unescape(_ code: UInt8) -> (delimiter: UInt8?, block: Int) {
        if code == 0xFF {
            return (nil, maxCobsBlockSize + 1)
        }

        let adjusted = Int(code) - Int(cobsCodeOffset)
        var delimiter = adjusted / maxCobsBlockSize
        var block = adjusted % maxCobsBlockSize
        if block == 0 {
            block = maxCobsBlockSize
            delimiter -= 1
        }
        return (UInt8(delimiter), block)
    }

    var decoded = [UInt8]()
    var state = unescape(first)

    for byte in bytes.dropFirst() {
        state.block -= 1
        if state.block > 0 {
            decoded.append(byte)
            continue
        }

        if let delimiter = state.delimiter {
            decoded.append(delimiter)
        }
        state = unescape(byte)
    }

    return decoded
}

private func crc32UpdateRaw(_ data: Data, seed: UInt32) -> UInt32 {
    var crc = seed ^ 0xFFFF_FFFF
    for byte in data {
        crc ^= UInt32(byte)
        for _ in 0 ..< 8 {
            if crc & 1 == 1 {
                crc = (crc >> 1) ^ 0xEDB8_8320
            } else {
                crc >>= 1
            }
        }
    }
    return crc ^ 0xFFFF_FFFF
}

private func crc32UpdatePaddedChunk(_ data: Data, seed: UInt32) -> UInt32 {
    let padding = (4 - (data.count % 4)) % 4
    if padding == 0 {
        return crc32UpdateRaw(data, seed: seed)
    }
    return crc32UpdateRaw(data + Data(repeating: 0, count: padding), seed: seed)
}

private func crc32ForFile(_ data: Data) -> UInt32 {
    let padding = (4 - (data.count % 4)) % 4
    return crc32UpdateRaw(data + Data(repeating: 0, count: padding), seed: 0)
}

@main
private struct SpikeBleHelper {
    static func main() async {
        do {
            let request = try parse(arguments: CommandLine.arguments)
            let controller = HubController()

            let output: Data
            switch request.command {
            case "scan":
                output = try encodeOutput(try await controller.scan(targetName: request.targetName))
            case "upload":
                output = try encodeOutput(try await controller.upload(request: request))
            case "stop":
                output = try encodeOutput(try await controller.stop(request: request))
            case "session":
                try await runSession(request: request, controller: controller)
                Foundation.exit(0)
            default:
                throw HelperError.usage("Unknown command: \(request.command)")
            }

            try writeOutput(output, to: request.outputPath)
            Foundation.exit(0)
        } catch {
            let outputPath = try? parse(arguments: CommandLine.arguments).outputPath
            writeError(error, to: outputPath ?? nil)
            Foundation.exit(1)
        }
    }
}
