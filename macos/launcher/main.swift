import AppKit
import Foundation
import WebKit

private enum TrackerSettings {
    static let dashboardURL = URL(string: "http://127.0.0.1:8765")!
    static let bookmarkKey = "TrackerProjectSecurityBookmarkV2"
    static let fallbackPathKey = "TrackerProjectPathFallback"
}

@MainActor
private final class TrackerAppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSToolbarDelegate {
    private enum ToolbarID {
        static let inbox = NSToolbarItem.Identifier("openInbox")
        static let reload = NSToolbarItem.Identifier("reloadDashboard")
    }

    private var window: NSWindow!
    private var webView: WKWebView!
    private var startupView: NSView!
    private let statusLabel = NSTextField(labelWithString: "Starting local-finance-tracker…")
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private let spinner = NSProgressIndicator()
    private lazy var retryButton = NSButton(title: "Try again", target: self, action: #selector(startService))

    private var projectURL: URL?
    private var hasScopedAccess = false
    private var serviceProcess: Process?
    private var serviceWasStartedHere = false
    private var hasLoadedDashboard = false
    private var startupTask: Task<Void, Never>?
    private var standardOutput: FileHandle?
    private var standardError: FileHandle?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        NSApp.activate(ignoringOtherApps: true)

        guard let project = chooseOrRestoreProjectFolder() else {
            showStartupError("Choose the local-finance-tracker project folder to continue.")
            return
        }
        projectURL = project
        startService()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { window.makeKeyAndOrderFront(nil) }
        if serviceProcess == nil { startService() }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        startupTask?.cancel()
        stopOwnedService()
        if hasScopedAccess, let projectURL {
            projectURL.stopAccessingSecurityScopedResource()
        }
    }

    @objc private func openInbox() {
        guard let projectURL else { return }
        NSWorkspace.shared.open(projectURL.appendingPathComponent("inbox", isDirectory: true))
    }

    @objc private func reloadDashboard() {
        guard !webView.isHidden else { startService(); return }
        webView.reload()
    }

    @objc private func quitTracker() { NSApp.terminate(nil) }

    @objc private func startService() {
        guard let projectURL else {
            guard let project = chooseOrRestoreProjectFolder() else {
                showStartupError("Choose the local-finance-tracker project folder to continue.")
                return
            }
            self.projectURL = project
            startService()
            return
        }

        startupTask?.cancel()
        showStarting("Starting the local service and inbox watcher…")
        startupTask = Task { [weak self] in
            guard let self else { return }
            if await dashboardIsAvailable(timeout: 1.5) {
                loadDashboard()
                return
            }
            do {
                try launchPythonService(in: projectURL)
            } catch {
                showStartupError("Could not start the local service.\n\(error.localizedDescription)")
                return
            }
            if await dashboardIsAvailable(timeout: 25.0) {
                loadDashboard()
            } else {
                stopOwnedService()
                showStartupError("The local service did not start. Check data/service.err.log in the local-finance-tracker folder.")
            }
        }
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = ""
        window.titleVisibility = .hidden
        window.appearance = NSAppearance(named: .darkAqua)
        window.titlebarAppearsTransparent = false
        window.backgroundColor = NSColor(calibratedRed: 22.0 / 255.0, green: 26.0 / 255.0, blue: 30.0 / 255.0, alpha: 1)
        window.titlebarSeparatorStyle = .line
        window.toolbarStyle = .unifiedCompact
        window.minSize = NSSize(width: 800, height: 560)
        window.center()
        window.isReleasedWhenClosed = false

        let root = NSView()
        root.wantsLayer = true
        root.layer?.backgroundColor = window.backgroundColor?.cgColor
        webView = WKWebView(frame: .zero)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.isHidden = true
        root.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            webView.topAnchor.constraint(equalTo: root.topAnchor),
            webView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        startupView = NSView()
        startupView.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(startupView)
        NSLayoutConstraint.activate([
            startupView.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            startupView.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            startupView.topAnchor.constraint(equalTo: root.topAnchor),
            startupView.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        ])

        spinner.style = .spinning
        spinner.controlSize = .regular
        statusLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        statusLabel.textColor = .labelColor
        detailLabel.font = .systemFont(ofSize: 12)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.alignment = .center
        detailLabel.maximumNumberOfLines = 4
        retryButton.isHidden = true

        let stack = NSStackView(views: [spinner, statusLabel, detailLabel, retryButton])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        startupView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: startupView.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: startupView.centerYAnchor),
            detailLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
        ])

        window.contentView = root
        let toolbar = NSToolbar(identifier: "TrackerToolbar")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        toolbar.sizeMode = .small
        window.toolbar = toolbar
        installAppMenu()
        window.makeKeyAndOrderFront(nil)
    }

    private func installAppMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        appItem.title = "local-finance-tracker"
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Open Inbox", action: #selector(openInbox), keyEquivalent: "i").target = self
        appMenu.addItem(withTitle: "Reload Dashboard", action: #selector(reloadDashboard), keyEquivalent: "r").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit local-finance-tracker", action: #selector(quitTracker), keyEquivalent: "q").target = self
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)

        let editItem = NSMenuItem()
        editItem.title = "Edit"
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)
        NSApp.mainMenu = mainMenu
    }

    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [ToolbarID.inbox, .flexibleSpace, ToolbarID.reload]
    }

    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [ToolbarID.inbox, .flexibleSpace, ToolbarID.reload]
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier itemIdentifier: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        let item = NSToolbarItem(itemIdentifier: itemIdentifier)
        if itemIdentifier == ToolbarID.inbox {
            item.label = "Inbox"
            item.paletteLabel = "Open Inbox Folder"
            item.image = NSImage(systemSymbolName: "tray.and.arrow.down", accessibilityDescription: "Inbox")
            item.target = self
            item.action = #selector(openInbox)
        } else if itemIdentifier == ToolbarID.reload {
            item.label = "Reload"
            item.paletteLabel = "Reload Dashboard"
            item.image = NSImage(systemSymbolName: "arrow.clockwise", accessibilityDescription: "Reload")
            item.target = self
            item.action = #selector(reloadDashboard)
        } else {
            return nil
        }
        return item
    }

    private func chooseOrRestoreProjectFolder() -> URL? {
        if let data = UserDefaults.standard.data(forKey: TrackerSettings.bookmarkKey) {
            var stale = false
            if let restored = try? URL(resolvingBookmarkData: data, options: [.withSecurityScope], relativeTo: nil,
                                       bookmarkDataIsStale: &stale), isValidProjectFolder(restored) {
                hasScopedAccess = restored.startAccessingSecurityScopedResource()
                if stale { saveProjectBookmark(restored) }
                return restored
            }
            UserDefaults.standard.removeObject(forKey: TrackerSettings.bookmarkKey)
        }
        if let path = UserDefaults.standard.string(forKey: TrackerSettings.fallbackPathKey) {
            let folder = URL(fileURLWithPath: path, isDirectory: true)
            if isValidProjectFolder(folder) { return folder }
        }

        let panel = NSOpenPanel()
        panel.title = "Choose the local-finance-tracker folder"
        panel.message = "This one-time choice lets local-finance-tracker access its local database and inbox."
        panel.prompt = "Choose"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if let path = Bundle.main.object(forInfoDictionaryKey: "TrackerProjectPath") as? String {
            panel.directoryURL = URL(fileURLWithPath: path, isDirectory: true)
        }
        guard panel.runModal() == .OK, let selected = panel.url, isValidProjectFolder(selected) else { return nil }
        hasScopedAccess = selected.startAccessingSecurityScopedResource()
        saveProjectBookmark(selected)
        return selected
    }

    private func saveProjectBookmark(_ folder: URL) {
        guard let data = try? folder.bookmarkData(options: [.withSecurityScope], includingResourceValuesForKeys: nil,
                                                  relativeTo: nil) else {
            UserDefaults.standard.set(folder.path, forKey: TrackerSettings.fallbackPathKey)
            return
        }
        UserDefaults.standard.set(data, forKey: TrackerSettings.bookmarkKey)
        UserDefaults.standard.set(folder.path, forKey: TrackerSettings.fallbackPathKey)
    }

    private func isValidProjectFolder(_ folder: URL) -> Bool {
        let manager = FileManager.default
        return manager.isExecutableFile(atPath: folder.appendingPathComponent(".venv/bin/python").path)
            && manager.fileExists(atPath: folder.appendingPathComponent("src/web.py").path)
            && manager.fileExists(atPath: folder.appendingPathComponent("data/local-finance-tracker.sqlite").path)
    }

    private func launchPythonService(in project: URL) throws {
        let dataFolder = project.appendingPathComponent("data", isDirectory: true)
        try FileManager.default.createDirectory(at: dataFolder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let stdoutURL = dataFolder.appendingPathComponent("service.out.log")
        let stderrURL = dataFolder.appendingPathComponent("service.err.log")
        if !FileManager.default.fileExists(atPath: stdoutURL.path) {
            FileManager.default.createFile(atPath: stdoutURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        if !FileManager.default.fileExists(atPath: stderrURL.path) {
            FileManager.default.createFile(atPath: stderrURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        let out = try FileHandle(forWritingTo: stdoutURL)
        let err = try FileHandle(forWritingTo: stderrURL)
        try out.seekToEnd()
        try err.seekToEnd()

        let process = Process()
        process.executableURL = project.appendingPathComponent(".venv/bin/python")
        process.arguments = ["-u", project.appendingPathComponent("src/web.py").path]
        process.currentDirectoryURL = project
        var environment = ProcessInfo.processInfo.environment
        // A Python env inherited from the app launcher (or a development shell)
        // can redirect this venv to another project's site-packages/pyvenv.cfg.
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "PYTHONUSERBASE")
        environment.removeValue(forKey: "VIRTUAL_ENV")
        environment["PYTHONUNBUFFERED"] = "1"
        environment["LFT_PORT"] = "8765"
        process.environment = environment
        process.standardOutput = out
        process.standardError = err
        process.terminationHandler = { [weak self] ended in
            DispatchQueue.main.async {
                guard let self, self.serviceProcess === ended else { return }
                self.serviceProcess = nil
                self.serviceWasStartedHere = false
                self.standardOutput = nil
                self.standardError = nil
                if ended.terminationStatus != 0 {
                    self.showStartupError("local-finance-tracker stopped unexpectedly. Check data/service.err.log in the project folder.")
                }
            }
        }
        try process.run()
        serviceProcess = process
        serviceWasStartedHere = true
        standardOutput = out
        standardError = err
    }

    private func stopOwnedService() {
        guard serviceWasStartedHere, let process = serviceProcess, process.isRunning else { return }
        process.interrupt()
        serviceWasStartedHere = false
    }

    private func dashboardIsAvailable(timeout: TimeInterval) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            if Task.isCancelled { return false }
            var request = URLRequest(url: TrackerSettings.dashboardURL)
            request.timeoutInterval = 1
            request.cachePolicy = .reloadIgnoringLocalCacheData
            if let (data, response) = try? await URLSession.shared.data(for: request),
               let http = response as? HTTPURLResponse, http.statusCode == 200,
               String(decoding: data.prefix(2048), as: UTF8.self).contains("local-finance-tracker") { return true }
            try? await Task.sleep(for: .milliseconds(500))
        } while Date() < deadline
        return false
    }

    private func loadDashboard() {
        startupView.isHidden = true
        webView.isHidden = false
        webView.load(URLRequest(url: TrackerSettings.dashboardURL))
    }

    private func showStarting(_ message: String) {
        webView.isHidden = true
        startupView.isHidden = false
        spinner.startAnimation(nil)
        retryButton.isHidden = true
        statusLabel.stringValue = "Starting local-finance-tracker…"
        detailLabel.stringValue = message
    }

    private func showStartupError(_ message: String) {
        webView.isHidden = true
        startupView.isHidden = false
        spinner.stopAnimation(nil)
        statusLabel.stringValue = "local-finance-tracker couldn’t start"
        detailLabel.stringValue = message
        retryButton.isHidden = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        hasLoadedDashboard = true
        window.title = "local-finance-tracker"
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        // WebKit reports a cancelled provisional load when a newer page load
        // replaces it (for example, quick navigation between app sections).
        // That is normal navigation, not a dashboard startup failure.
        if (error as NSError).code == NSURLErrorCancelled { return }
        // Once the app is running, a transient page load failure should leave
        // the last usable page visible instead of replacing it with startup UI.
        if hasLoadedDashboard { return }
        showStartupError("Could not load the local dashboard. Check that the server is running, then try again.")
    }

    private func isLocalDashboardURL(_ url: URL) -> Bool {
        url.scheme == "http" && (url.host == "127.0.0.1" || url.host == "localhost") && url.port == 8765
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if isLocalDashboardURL(url) {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
            if url.scheme == "http" || url.scheme == "https" {
                NSWorkspace.shared.open(url)
            }
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil, let url = navigationAction.request.url else { return nil }
        if isLocalDashboardURL(url) {
            webView.load(navigationAction.request)
        } else if url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        return nil
    }
}

@main
private struct TrackerNativeApp {
    @MainActor
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.regular)
        let delegate = TrackerAppDelegate()
        app.delegate = delegate
        app.run()
    }
}
