import Foundation
import Security

let sourceService = "Claude Code-credentials"

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(("claude_profile_vault: " + message + "\n").utf8))
    exit(2)
}

guard CommandLine.arguments.count == 3,
      ["capture", "percent"].contains(CommandLine.arguments[1]) else {
    fail("usage: claude_profile_vault.swift <capture|percent> <c1|c2|c3>")
}

let action = CommandLine.arguments[1]
let profile = CommandLine.arguments[2].lowercased()
guard ["c1", "c2", "c3"].contains(profile) else {
    fail("profile must be c1, c2, or c3")
}

let destinationService = "SmartWeatherClock-Claude-" + profile.uppercased()
let account = NSUserName()
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: destinationService,
    kSecAttrAccount as String: account,
]
if action == "capture" {
    let reader = Process()
    reader.executableURL = URL(fileURLWithPath: "/usr/bin/security")
    reader.arguments = ["find-generic-password", "-s", sourceService, "-w"]
    var environment = ProcessInfo.processInfo.environment
    environment["HOME"] = NSHomeDirectoryForUser(NSUserName()) ?? NSHomeDirectory()
    reader.environment = environment
    let output = Pipe()
    reader.standardOutput = output
    reader.standardError = Pipe()
    do {
        try reader.run()
        reader.waitUntilExit()
    } catch {
        fail("could not start macOS Keychain")
    }
    guard reader.terminationStatus == 0 else {
        fail("shared Claude Code credential is unavailable")
    }
    let credential = output.fileHandleForReading.readDataToEndOfFile()
    guard !credential.isEmpty else {
        fail("shared Claude Code credential is empty")
    }
    var result = SecItemUpdate(query as CFDictionary, [kSecValueData as String: credential] as CFDictionary)
    if result == errSecItemNotFound {
        var newItem = query
        newItem[kSecValueData as String] = credential
        result = SecItemAdd(newItem as CFDictionary, nil)
    }
    guard result == errSecSuccess else {
        fail("could not store private Keychain credential (status \(result))")
    }
    print("claude profile vault: \(profile.uppercased()) captured")
    exit(0)
}

var readQuery = query
readQuery[kSecReturnData as String] = true
readQuery[kSecMatchLimit as String] = kSecMatchLimitOne
var item: CFTypeRef?
guard SecItemCopyMatching(readQuery as CFDictionary, &item) == errSecSuccess,
      let credential = item as? Data,
      let credentialJson = try? JSONSerialization.jsonObject(with: credential) as? [String: Any],
      let oauth = credentialJson["claudeAiOauth"] as? [String: Any],
      let token = oauth["accessToken"] as? String else {
    fail("private Keychain credential is unavailable")
}

var request = URLRequest(url: URL(string: "https://api.anthropic.com/api/oauth/usage")!)
request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
request.setValue("oauth-2025-04-20", forHTTPHeaderField: "anthropic-beta")
request.setValue("claude-code-usage-collector/1.0", forHTTPHeaderField: "User-Agent")
let semaphore = DispatchSemaphore(value: 0)
var responseData: Data?
var responseError: Error?
var responseStatus: Int?
URLSession.shared.dataTask(with: request) { data, urlResponse, error in
    responseData = data
    responseError = error
    responseStatus = (urlResponse as? HTTPURLResponse)?.statusCode
    semaphore.signal()
}.resume()
guard semaphore.wait(timeout: .now() + 20) == .success else {
    fail("Claude usage request timed out")
}
if let error = responseError {
    fail("Claude usage request network error: \(error.localizedDescription)")
}
guard responseStatus == 200, let data = responseData else {
    fail("Claude usage request returned HTTP \(responseStatus.map(String.init) ?? "no response")")
}
guard let response = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let fiveHour = response["five_hour"] as? [String: Any],
      let utilization = fiveHour["utilization"] as? NSNumber,
      CFGetTypeID(utilization) != CFBooleanGetTypeID(),
      utilization.doubleValue.isFinite else {
    fail("Claude usage response format is unsupported")
}
let percentage = Int(utilization.doubleValue.rounded())
guard (0...100).contains(percentage) else {
    fail("Claude usage percentage is out of range")
}

var resetMinutes: Int?
if let resetsAt = fiveHour["resets_at"] as? String {
    let withFractional = ISO8601DateFormatter()
    withFractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let plain = ISO8601DateFormatter()
    if let moment = withFractional.date(from: resetsAt) ?? plain.date(from: resetsAt) {
        let remaining = Int(moment.timeIntervalSinceNow / 60)
        if remaining >= 0 {
            resetMinutes = min(remaining, 65535)
        }
    }
}
if let resetMinutes {
    print("{\"percent\":\(percentage),\"reset_minutes\":\(resetMinutes)}")
} else {
    print("{\"percent\":\(percentage)}")
}
