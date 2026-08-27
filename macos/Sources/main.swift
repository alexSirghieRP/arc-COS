// Chief of Staff — native macOS shell.
//
// A real Mac app around the CoS board:
//   • owns the backend: starts uvicorn if :7777 is closed, stops it on quit
//     (only if it started it — a dev server you launched yourself is left alone)
//   • Dock badge = active high-severity pipeline-monitor alerts
//   • menu-bar status item = live pipeline health dot (green/amber/red)
//   • native macOS notifications for new high-severity pipeline problems
//   • external links open in the default browser; the board stays in-app
//
// Built by build.sh into "Chief of Staff.app" (ad-hoc signed).

import Cocoa
import WebKit
import UserNotifications

let PORT: UInt16 = 7777
let BASE = "http://127.0.0.1:7777"
let REPO = "\(NSHomeDirectory())/arc-cos"   // where you cloned the repo

func portOpen(_ port: UInt16) -> Bool {
    let sock = socket(AF_INET, SOCK_STREAM, 0)
    if sock < 0 { return false }
    defer { close(sock) }
    var tv = timeval(tv_sec: 0, tv_usec: 300_000)
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let r = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return r == 0
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var statusItem: NSStatusItem!
    var backend: Process?
    var pollTimer: Timer?
    var notifiedKeys = Set<String>()
    var lastStatus = "unknown"

    // ── lifecycle ──────────────────────────────────────────────────────────

    func applicationDidFinishLaunching(_ note: Notification) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
        if !portOpen(PORT) { startBackend() }
        buildMenu()
        buildWindow()
        buildStatusItem()
        showSplash()
        loadWhenReady(attempt: 0)
        pollTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in self?.poll() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { [weak self] in self?.poll() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationWillTerminate(_ note: Notification) {
        backend?.terminate()   // only set if WE started the server
    }

    // ── backend ────────────────────────────────────────────────────────────

    func startBackend() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        // Login shell so gcloud etc resolve from the user's PATH — but a
        // NON-interactive login shell never sources .zshrc, which is where
        // ~/.local/bin (i.e. uv) gets added. Launched from Finder/Dock we start
        // from the bare system PATH and `uv` is not found, so source uv's own
        // env file first.
        p.arguments = ["-lc",
            "[ -f \"$HOME/.local/bin/env\" ] && . \"$HOME/.local/bin/env\"; cd \"\(REPO)/backend\" && exec uv run uvicorn app.main:app --port \(PORT) --host 127.0.0.1 >> /tmp/chief-app-backend.log 2>&1"]
        do { try p.run(); backend = p } catch {
            NSLog("backend start failed: \(error)")
        }
    }

    // ── window / webview ───────────────────────────────────────────────────

    func buildWindow() {
        let cfg = WKWebViewConfiguration()
        cfg.preferences.setValue(true, forKey: "developerExtrasEnabled")
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        // Off: whole-page pinch-zoom would compete with (and can swallow, at the OS
        // gesture-recognizer level) the trackpad pinch that the arch-map diagrams use
        // for their own in-page zoom. The board is a fixed dashboard layout, not
        // reflowable content that benefits from OS-level magnification.
        web.allowsMagnification = false

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1680, height: 1020),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Chief of Staff"
        window.minSize = NSSize(width: 980, height: 640)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("ChiefMainWindow")
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showSplash() {
        web.loadHTMLString("""
        <html><body style="margin:0;background:#09090b;display:flex;align-items:center;justify-content:center;height:100vh;font-family:-apple-system">
        <div style="text-align:center">
          <div style="width:52px;height:52px;margin:0 auto 18px;border-radius:14px;background:linear-gradient(135deg,#6C5CE7,#a29bfe);display:flex;align-items:center;justify-content:center">
            <span style="color:white;font-size:26px;font-weight:800">⌁</span></div>
          <div style="color:#e4e4e7;font-size:15px;font-weight:600">Chief of Staff</div>
          <div id="s" style="color:#52525b;font-size:12px;margin-top:6px">starting backend…</div>
        </div></body></html>
        """, baseURL: nil)
    }

    func loadWhenReady(attempt: Int) {
        if portOpen(PORT) {
            web.load(URLRequest(url: URL(string: BASE)!))
            return
        }
        if attempt > 240 {   // ~2 min — MCP init can be slow
            web.loadHTMLString("<html><body style='background:#09090b;color:#f87171;font-family:-apple-system;display:flex;align-items:center;justify-content:center;height:100vh'>Backend didn't start — check /tmp/chief-app-backend.log</body></html>", baseURL: nil)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.loadWhenReady(attempt: attempt + 1)
        }
    }

    // external links → default browser; the board itself stays in-app
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, let host = url.host,
           host != "127.0.0.1" && host != "localhost" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // target=_blank links
    func webView(_ webView: WKWebView, createWebViewWith cfg: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // ── menu bar (status item) ─────────────────────────────────────────────

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        updateStatusDot("unknown", detail: "connecting…")
        let menu = NSMenu()
        menu.addItem(withTitle: "Open Chief of Staff", action: #selector(openMain), keyEquivalent: "")
        menu.addItem(withTitle: "Poll pipeline now", action: #selector(pollNow), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        statusItem.menu = menu
    }

    func updateStatusDot(_ status: String, detail: String) {
        let colors: [String: NSColor] = [
            "green": .systemGreen, "amber": .systemOrange, "red": .systemRed]
        let dot = NSAttributedString(
            string: "●",
            attributes: [.foregroundColor: colors[status] ?? NSColor.systemGray,
                         .font: NSFont.systemFont(ofSize: 12)])
        statusItem.button?.attributedTitle = dot
        statusItem.button?.toolTip = "Pipeline monitor: \(status) — \(detail)"
    }

    @objc func openMain() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func pollNow() {
        var req = URLRequest(url: URL(string: "\(BASE)/api/osca/refresh")!)
        req.httpMethod = "POST"
        URLSession.shared.dataTask(with: req).resume()
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.poll() }
    }

    @objc func reloadPage() { web.reload() }

    // ── native surface: badge + notifications ─────────────────────────────

    func fetchJSON(_ path: String, done: @escaping ([String: Any]) -> Void) {
        guard let url = URL(string: "\(BASE)\(path)") else { return }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
            DispatchQueue.main.async { done(obj) }
        }.resume()
    }

    func poll() {
        guard portOpen(PORT) else {
            updateStatusDot("unknown", detail: "backend offline")
            return
        }
        fetchJSON("/api/osca/health") { [weak self] h in
            guard let self else { return }
            let status = (h["status"] as? String) ?? "unknown"
            let totals = h["totals"] as? [String: Any] ?? [:]
            let esc = (totals["escalations"] as? Int) ?? 0
            let failing = (totals["checks_failing"] as? Int) ?? 0
            self.updateStatusDot(status, detail: "\(esc) escalated · \(failing) checks failing")
            self.lastStatus = status
        }
        fetchJSON("/api/board") { [weak self] b in
            guard let self else { return }
            let alerts = (b["osca_alerts"] as? Int) ?? 0
            NSApp.dockTile.badgeLabel = alerts > 0 ? "\(alerts)" : ""
            for item in (b["osca_new"] as? [[String: Any]]) ?? [] {
                guard let key = item["key"] as? String, !self.notifiedKeys.contains(key) else { continue }
                self.notifiedKeys.insert(key)
                let content = UNMutableNotificationContent()
                content.title = "Pipeline monitor — failure detected"
                var body = (item["title"] as? String) ?? key
                if let n = item["count"] as? Int, n > 1 { body += " (\(n) affected)" }
                content.body = body
                content.sound = .default
                UNUserNotificationCenter.current().add(
                    UNNotificationRequest(identifier: key, content: content, trigger: nil))
            }
        }
    }

    // ── menus ──────────────────────────────────────────────────────────────

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Chief of Staff",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Chief of Staff",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(NSMenuItem.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reloadPage), keyEquivalent: "r")
        view.addItem(NSMenuItem.separator())
        view.addItem(withTitle: "Enter Full Screen",
                     action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = win

        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
