import Cocoa
import WebKit
import UniformTypeIdentifiers

// MARK: - Drop-target container view
final class DropView: NSView {
    var onFileURL: ((URL) -> Void)?
    override init(frame: NSRect) { super.init(frame: frame); registerForDraggedTypes([.fileURL]) }
    required init?(coder: NSCoder) { super.init(coder: coder); registerForDraggedTypes([.fileURL]) }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        (sender.draggingPasteboard.types?.contains(.fileURL) ?? false) ? .copy : []
    }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self],
                    options: [.urlReadingFileURLsOnly: true])?.compactMap { $0 as? URL } ?? []
        guard let url = urls.first else { return false }
        onFileURL?(url); return true
    }
}

// MARK: - Main controller
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
                         WKScriptMessageHandler, NSWindowDelegate, NSToolbarDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var statusLabel: NSTextField?
    var modeSeg: NSSegmentedControl?
    var modeMenuItem: NSMenuItem?
    var formatPopover: NSPopover?
    var recentSubmenu: NSMenu?
    var currentURL: URL?
    var pendingURL: URL?
    var pendingShot = false
    private let shotMode = ProcessInfo.processInfo.environment["PREE_SHOT"] == "1"
    var closeAfterSave = false
    var editMode = false
    var isDirty = false { didSet { refreshChrome() } }

    let palette: [(String, String)] = [
        ("White", "#FFFFFF"), ("Yellow", "#FFD166"), ("Orange", "#FF9F43"), ("Red", "#FF6B6B"),
        ("Green", "#51CF66"), ("Blue", "#4DABF7"), ("Purple", "#B197FC"), ("Gray", "#ADB5BD"), ("Black", "#343A40")
    ]

    // ---------- Editor script injected into pages ----------
    static let editorJS = """
    (function(){
      try { document.execCommand('styleWithCSS', false, true); } catch(e) {}
      var armed = false, timer = null;
      function mark(){
        if (!armed) return;
        clearTimeout(timer);
        timer = setTimeout(function(){
          try { window.webkit.messageHandlers.host.postMessage('dirty'); } catch(e) {}
        }, 250);
      }
      function arm(){ armed = true; }
      document.addEventListener('input', function(){ arm(); mark(); }, true);
      document.addEventListener('keydown', arm, true);
      document.addEventListener('drop', function(){ setTimeout(function(){ arm(); mark(); }, 0); }, true);
      try {
        new MutationObserver(function(){ mark(); })
          .observe(document.documentElement, { childList: true, subtree: true, characterData: true });
      } catch(e) {}
    })();
    """

    // ---------- Launch ----------
    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua)
        window.title = "Pree HTML Station"
        window.minSize = NSSize(width: 720, height: 500)
        window.backgroundColor = NSColor(red: 0.05, green: 0.07, blue: 0.09, alpha: 1)
        window.delegate = self

        // Center the window on the screen under the mouse (multi-display safe)
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first(where: { NSMouseInRect(mouse, $0.frame, false) }) ?? NSScreen.screens.first
        if let v = screen?.visibleFrame {
            let w = min(1280, max(900, v.width - 80))
            let h = min(860, max(600, v.height - 80))
            window.setFrame(NSRect(x: v.midX - w/2, y: v.midY - h/2, width: w, height: h), display: false)
        }

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "host")
        config.userContentController.addUserScript(
            WKUserScript(source: Self.editorJS, injectionTime: .atDocumentEnd, forMainFrameOnly: true))

        let container = DropView(frame: NSRect(x: 0, y: 0, width: 1280, height: 860))
        webView = WKWebView(frame: container.bounds, configuration: config)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.underPageBackgroundColor = NSColor(red: 0.05, green: 0.07, blue: 0.09, alpha: 1)
        container.addSubview(webView)
        container.onFileURL = { [weak self] url in self?.loadFile(url) }
        window.contentView = container

        let toolbar = NSToolbar(identifier: "main")
        toolbar.delegate = self
        toolbar.displayMode = .iconOnly
        window.toolbar = toolbar

        refreshChrome()

        if let u = pendingURL { loadFile(u) }
        else if let u = argURL() { loadFile(u) }
        else { showWelcome() }

        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(webView)
        NSApp.activate(ignoringOtherApps: true)
    }

    func application(_ application: NSApplication, openFile filename: String) -> Bool {
        let u = URL(fileURLWithPath: filename)
        if webView == nil { pendingURL = u } else { loadFile(u) }
        return true
    }
    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { false }
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window?.makeKeyAndOrderFront(nil) }
        return true
    }

    // Quit protection: prompt when there are unsaved changes
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard isDirty, window != nil else { return .terminateNow }
        let a = NSAlert()
        a.messageText = "Unsaved changes"
        a.informativeText = currentURL?.lastPathComponent ?? ""
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Don't Save")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { [weak self] resp in
            guard let self else { return NSApp.reply(toApplicationShouldTerminate: false) }
            switch resp {
            case .alertFirstButtonReturn:
                self.performSave { NSApp.reply(toApplicationShouldTerminate: true) }
            case .alertSecondButtonReturn:
                self.isDirty = false
                NSApp.reply(toApplicationShouldTerminate: true)
            default:
                NSApp.reply(toApplicationShouldTerminate: false)
            }
        }
        return .terminateLater
    }

    private func argURL() -> URL? {
        let fm = FileManager.default
        for a in ProcessInfo.processInfo.arguments.dropFirst() {
            if a.hasPrefix("-") { continue }
            let u = URL(fileURLWithPath: a)
            if fm.fileExists(atPath: u.path), ["html", "htm"].contains(u.pathExtension.lowercased()) { return u }
        }
        return nil
    }

    // ---------- Open / Save ----------
    func loadFile(_ url: URL) {
        let fm = FileManager.default
        guard fm.fileExists(atPath: url.path) else { warn("File not found: \(url.path)"); return }
        currentURL = url
        isDirty = false
        editMode = false
        closeAfterSave = false
        pendingShot = shotMode
        // Allow reading relative assets (images/CSS) under the home directory;
        // fall back to the file's own folder otherwise
        let home = URL(fileURLWithPath: NSHomeDirectory())
        let access = url.standardizedFileURL.path.hasPrefix(home.path) ? home : url.deletingLastPathComponent()
        webView.loadFileURL(url, allowingReadAccessTo: access)
        rememberRecent(url)
        refreshChrome()
    }

    // ---------- Recent files ----------
    private func rememberRecent(_ url: URL) {
        var recents = UserDefaults.standard.stringArray(forKey: "recentFiles") ?? []
        recents.removeAll { $0 == url.path }
        recents.insert(url.path, at: 0)
        UserDefaults.standard.set(Array(recents.prefix(8)), forKey: "recentFiles")
        rebuildRecentMenu()
    }

    private func rebuildRecentMenu() {
        guard let m = recentSubmenu else { return }
        m.removeAllItems()
        let recents = UserDefaults.standard.stringArray(forKey: "recentFiles") ?? []
        if recents.isEmpty {
            let mi = m.addItem(withTitle: "No Recent Files", action: nil, keyEquivalent: "")
            mi.isEnabled = false
            return
        }
        for p in recents {
            let mi = m.addItem(withTitle: URL(fileURLWithPath: p).lastPathComponent,
                               action: #selector(recentOpen(_:)), keyEquivalent: "")
            mi.representedObject = p
            mi.toolTip = p
        }
        m.addItem(.separator())
        m.addItem(withTitle: "Clear Menu", action: #selector(clearRecents), keyEquivalent: "")
    }

    @objc func recentOpen(_ sender: NSMenuItem) {
        if let p = sender.representedObject as? String { loadFile(URL(fileURLWithPath: p)) }
    }

    @objc func clearRecents() {
        UserDefaults.standard.removeObject(forKey: "recentFiles")
        rebuildRecentMenu()
        if currentURL == nil { showWelcome() }
    }

    static func escHTML(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
         .replacingOccurrences(of: "<", with: "&lt;")
         .replacingOccurrences(of: ">", with: "&gt;")
    }

    static func jsEscape(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
         .replacingOccurrences(of: "'", with: "\\'")
         .replacingOccurrences(of: "\"", with: "\\\"")
    }

    @objc func openPanel(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [UTType.html]
        panel.message = "Choose an HTML file to edit"
        panel.directoryURL = currentURL?.deletingLastPathComponent()
            ?? URL(fileURLWithPath: NSHomeDirectory() + "/Desktop")
        panel.beginSheetModal(for: window) { [weak self] resp in
            guard let self, resp == .OK, let url = panel.url else { return }
            self.loadFile(url)
        }
    }

    @objc func saveDoc() { performSave(completion: nil) }

    func performSave(completion: (() -> Void)?) {
        guard let url = currentURL else { saveAsDoc(); return }
        let js = "'<!DOCTYPE html>\\n' + document.documentElement.outerHTML"
        webView.evaluateJavaScript(js) { [weak self] res, err in
            guard let self else { return }
            if let html = res as? String, let data = html.data(using: .utf8) {
                do {
                    try data.write(to: url, options: .atomic)
                    self.isDirty = false
                    if self.closeAfterSave {
                        self.closeAfterSave = false
                        self.window.performClose(nil)
                    }
                    completion?()
                } catch { self.warn("Save failed: \(error.localizedDescription)") }
            } else {
                self.warn("Save failed: could not read edited content \(err?.localizedDescription ?? "")")
            }
        }
    }

    @objc func saveAsDoc() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [UTType.html]
        panel.nameFieldStringValue = currentURL?.lastPathComponent ?? "Untitled.html"
        panel.directoryURL = currentURL?.deletingLastPathComponent()
        panel.beginSheetModal(for: window) { [weak self] resp in
            guard let self, resp == .OK, let url = panel.url else { return }
            self.currentURL = url
            self.performSave(completion: nil)
        }
    }

    @objc func previewInBrowser() {
        guard let u = currentURL else { warn("Open a file first"); return }
        if isDirty { performSave { NSWorkspace.shared.open(u) } }
        else { NSWorkspace.shared.open(u) }
    }

    // ---------- Read / Edit mode ----------
    @objc func toggleMode() { setEditMode(!editMode) }

    @objc func modeChanged(_ sender: NSSegmentedControl) {
        setEditMode(sender.selectedSegment == 1)
    }

    func setEditMode(_ on: Bool) {
        guard editMode != on else { return }
        editMode = on
        let js = on ? "document.designMode='on'"
                    : "document.designMode='off'; try{window.getSelection().removeAllRanges();}catch(e){}"
        webView.evaluateJavaScript(js, completionHandler: nil)
        refreshChrome()
    }

    // Drawer: B / I / U / S / Highlight tucked behind a single toolbar button
    @objc func toggleFormat(_ sender: NSButton) {
        if let p = formatPopover, p.isShown { p.performClose(sender); return }
        let container = NSView(frame: NSRect(x: 0, y: 0, width: 5 * 40 + 4 * 8 + 24, height: 56))
        var x: CGFloat = 12
        let items: [(String, Selector, String)] = [
            ("bold", #selector(execBold), "Bold (⌘B)"),
            ("italic", #selector(execItalic), "Italic (⌘I)"),
            ("underline", #selector(execUnderline), "Underline (⌘U)"),
            ("strikethrough", #selector(execStrike), "Strikethrough"),
            ("highlighter", #selector(execHighlight), "Highlight"),
        ]
        for (sym, action, tip) in items {
            let b = NSButton(frame: NSRect(x: x, y: 12, width: 40, height: 32))
            b.bezelStyle = .texturedRounded
            if let img = NSImage(systemSymbolName: sym, accessibilityDescription: tip) {
                b.image = img
                b.imageScaling = .scaleProportionallyDown
            }
            b.target = self
            b.action = action
            b.toolTip = tip
            container.addSubview(b)
            x += 48
        }
        let vc = NSViewController()
        vc.view = container
        let p = NSPopover()
        p.contentViewController = vc
        p.behavior = .transient
        p.show(relativeTo: sender.bounds, of: sender, preferredEdge: .minY)
        formatPopover = p
    }

    @objc func reloadDoc() {
        guard currentURL != nil else { return }
        if isDirty {
            let a = NSAlert()
            a.messageText = "Unsaved changes will be discarded. Reload anyway?"
            a.addButton(withTitle: "Reload")
            a.addButton(withTitle: "Cancel")
            a.beginSheetModal(for: window) { [weak self] r in
                if r == .alertFirstButtonReturn { self?.editMode = false; self?.webView.reload() }
            }
        } else { editMode = false; webView.reload() }
    }

    private func showWelcome() {
        var recentRows = ""
        for p in (UserDefaults.standard.stringArray(forKey: "recentFiles") ?? []).prefix(5) {
            let f = URL(fileURLWithPath: p)
            let name = Self.escHTML(f.lastPathComponent)
            let raw = f.deletingLastPathComponent().path
            let home = NSHomeDirectory()
            let short = raw.hasPrefix(home) ? "~" + raw.dropFirst(home.count) : raw
            let dir = Self.escHTML(short)
            let jp = Self.jsEscape(p)
            recentRows += "<div class=\"rrow\" onclick=\"post('openpath','\(jp)')\"><b>\(name)</b><span>\(dir)</span></div>"
        }
        let recentBlock = recentRows.isEmpty ? "" : "<div class=\"recent\"><div class=\"rtitle\">Recent files</div>\(recentRows)</div>"
        let html = """
        <!DOCTYPE html><html><head><meta charset="utf-8"><style>
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0d1117;color:#e6edf3;font-family:-apple-system,"Helvetica Neue",sans-serif;overflow:hidden}
        .hero{text-align:center;max-width:560px;padding:0 24px}
        .hero>*{animation:rise .7s cubic-bezier(.22,1,.36,1) both}
        .hero>*:nth-child(2){animation-delay:.08s}
        .hero>*:nth-child(3){animation-delay:.16s}
        .hero>*:nth-child(4){animation-delay:.24s}
        .hero>*:nth-child(5){animation-delay:.32s}
        @keyframes rise{from{transform:translateY(16px)}to{transform:translateY(0)}}
        .glyph{font-family:Menlo,monospace;font-weight:700;font-size:54px;letter-spacing:-2px}
        .glyph .b{color:#38BDF8}
        .bar{width:64px;height:6px;border-radius:3px;background:linear-gradient(90deg,#38BDF8,#818CF8);margin:20px auto 26px}
        h1{font-size:26px;margin:0 0 8px;font-weight:700;letter-spacing:-.3px}
        .tag{color:#8b949e;font-size:15px;margin:0 0 34px}
        .drop{border:1.5px dashed #30363d;border-radius:14px;padding:30px 46px;cursor:pointer;transition:border-color .18s ease,background .18s ease,transform .18s ease}
        .drop:hover{border-color:#38BDF8;background:rgba(56,189,248,.05);transform:translateY(-1px)}
        .drop b{font-size:16px}
        .drop p{color:#8b949e;font-size:13.5px;margin:6px 0 0}
        kbd{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:2px 8px;font-family:Menlo,monospace;font-size:12.5px;color:#79c0ff}
        .hints{color:#8b949e;font-size:13px;margin-top:26px}
        .hints span{margin:0 7px}
        .recent{margin-top:30px;text-align:left;width:100%}
        .rtitle{color:#8b949e;font-size:11px;letter-spacing:1.2px;text-transform:uppercase;margin-bottom:6px}
        .rrow{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding:8px 14px;border-radius:8px;cursor:pointer;transition:background .15s ease}
        .rrow:hover{background:#161b22}
        .rrow b{font-size:13.5px;font-weight:600}
        .rrow span{color:#8b949e;font-size:12px;font-family:Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
        </style></head><body><div class="hero">
        <div class="glyph"><span class="b">&lt;</span>/<span class="b">&gt;</span></div>
        <div class="bar"></div>
        <h1>Pree HTML Station</h1>
        <p class="tag">An HTML editor for macOS.</p>
        <div class="drop" onclick="post('open','')">
          <b>Drop a .html file here</b>
          <p>or click to browse — ⌘O works too</p>
        </div>
        \(recentBlock)
        <p class="hints"><span><kbd>⌘E</kbd> Edit like a doc</span><span><kbd>⌘S</kbd> Save to original</span><span><kbd>⌃⌘B</kbd> Preview</span></p>
        </div><script>function post(k,v){window.webkit.messageHandlers.host.postMessage({kind:k,path:v})}</script></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // ---------- Editing commands ----------
    @objc func execBold()        { exec("bold");        isDirty = true }
    @objc func execItalic()      { exec("italic");      isDirty = true }
    @objc func execUnderline()   { exec("underline");   isDirty = true }
    @objc func execStrike()      { exec("strikeThrough"); isDirty = true }
    @objc func execHighlight()   { exec("hiliteColor", "#FFD54A"); isDirty = true }

    @objc func applyBlock(_ sender: NSMenuItem) {
        if let blk = sender.representedObject as? String { exec("formatBlock", blk); isDirty = true }
    }

    @objc func colorSelected(_ sender: Any?) {
        var hex: String?
        if let mi = sender as? NSMenuItem {
            hex = mi.representedObject as? String
        } else if let pop = sender as? NSPopUpButton, let mi = pop.selectedItem {
            hex = mi.representedObject as? String
        }
        if let hex { exec("foreColor", hex); isDirty = true }
    }

    private func exec(_ cmd: String, _ arg: String? = nil) {
        let js: String
        if let arg {
            js = "document.execCommand('\(cmd)', false, '\(arg)')"
        } else {
            js = "document.execCommand('\(cmd)')"
        }
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    @objc func showHelp() {
        let a = NSAlert()
        a.messageText = "How to use"
        a.informativeText = """
        1. ⌘O to open any .html file (or drag it into the window)
        2. Read mode by default: clicks never edit, links are clickable
        3. Press ⌘E or use the Read | Edit toggle,
           then click any text and type, like a Word doc
        4. ⌘S saves back to the original file · ⇧⌘S Save As
        5. Rightmost toolbar button: auto-save, then preview in your browser
        """
        a.runModal()
    }

    private func warn(_ msg: String) {
        let a = NSAlert()
        a.messageText = msg
        a.runModal()
    }

    // ---------- Status display ----------
    private func refreshChrome() {
        guard window != nil else { return }
        if let url = currentURL {
            window.title = url.lastPathComponent + (isDirty ? " •" : "") + (editMode ? " — Edited" : "")
        } else {
            window.title = "Pree HTML Station"
        }
        modeSeg?.selectedSegment = editMode ? 1 : 0
        modeMenuItem?.state = editMode ? .on : .off
        if let label = statusLabel {
            if currentURL == nil {
                label.stringValue = "No file open"
                label.textColor = .secondaryLabelColor
            } else if isDirty {
                label.stringValue = "● Unsaved"
                label.textColor = .systemOrange
            } else {
                label.stringValue = "✓ Saved"
                label.textColor = .systemGreen
            }
        }
    }

    // ---------- JS messages ----------
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        if (message.body as? String) == "dirty" { isDirty = true; return }
        guard let d = message.body as? [String: String] else { return }
        switch d["kind"] {
        case "open": openPanel(nil)          // welcome page drop-card click
        case "openpath":                     // welcome page recent-files click
            if let p = d["path"] { loadFile(URL(fileURLWithPath: p)) }
        default: break
        }
    }

    // ---------- Navigation interception ----------
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard action.navigationType == .linkActivated, let url = action.request.url else {
            decisionHandler(.allow); return
        }
        // Same-file anchor (TOC jump): let WebKit scroll natively
        if url.isFileURL, url.standardizedFileURL == currentURL?.standardizedFileURL {
            decisionHandler(.allow); return
        }
        // Local .html link: open in this window (doc browsing in read mode)
        if url.isFileURL, ["html", "htm"].contains(url.pathExtension.lowercased()) {
            if isDirty {
                let a = NSAlert()
                a.messageText = "Save unsaved changes before opening the new file?"
                a.addButton(withTitle: "Save & Open")
                a.addButton(withTitle: "Don't Save")
                a.addButton(withTitle: "Cancel")
                a.beginSheetModal(for: window) { [weak self] resp in
                    guard let self else { decisionHandler(.cancel); return }
                    switch resp {
                    case .alertFirstButtonReturn:
                        self.performSave { self.loadFile(url) }
                        decisionHandler(.cancel)
                    case .alertSecondButtonReturn:
                        self.isDirty = false
                        self.loadFile(url)
                        decisionHandler(.cancel)
                    default:
                        decisionHandler(.cancel)
                    }
                }
            } else {
                loadFile(url)
                decisionHandler(.cancel)
            }
            return
        }
        // Everything else (http/images/PDF...): hand to the system default app
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    // Screenshot staging hook (README hero): PREE_SHOT=1 + a #shot-select span in the page
    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        guard pendingShot, currentURL != nil else { return }
        pendingShot = false
        setEditMode(true)
        webView.evaluateJavaScript("""
            (function(){
              var el = document.getElementById('shot-select');
              if (!el) { return 'no-span'; }
              el.style.background = 'rgba(9,105,218,0.5)';
              el.style.borderRadius = '3px';
              el.style.color = '#ffffff';
              return 'ok';
            })();
        """, completionHandler: nil)
        isDirty = true
    }

    // ---------- Close protection ----------
    func windowShouldClose(_ s: NSWindow) -> Bool {
        guard isDirty else { return true }
        let a = NSAlert()
        a.messageText = "Unsaved changes"
        a.informativeText = currentURL?.lastPathComponent ?? ""
        a.addButton(withTitle: "Save")
        a.addButton(withTitle: "Don't Save")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { [weak self] resp in
            guard let self else { return }
            switch resp {
            case .alertFirstButtonReturn:
                self.closeAfterSave = true
                self.performSave(completion: nil)
            case .alertSecondButtonReturn:
                self.isDirty = false
                s.performClose(nil)
            default: break
            }
        }
        return false
    }

    // ---------- Menu ----------
    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About Pree HTML Station",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let fileItem = NSMenuItem(); main.addItem(fileItem)
        let file = NSMenu(title: "File"); fileItem.submenu = file
        file.addItem(withTitle: "Open…", action: #selector(openPanel(_:)), keyEquivalent: "o")
        let recentItem = file.addItem(withTitle: "Open Recent", action: nil, keyEquivalent: "")
        let recentMenu = NSMenu(title: "Open Recent")
        recentItem.submenu = recentMenu
        recentSubmenu = recentMenu
        rebuildRecentMenu()
        file.addItem(.separator())
        file.addItem(withTitle: "Save", action: #selector(saveDoc), keyEquivalent: "s")
        let saveAsMi = file.addItem(withTitle: "Save As…", action: #selector(saveAsDoc), keyEquivalent: "s")
        saveAsMi.keyEquivalentModifierMask = [.command, .shift]
        file.addItem(.separator())
        file.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "Edit"); editItem.submenu = edit
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redoMi = edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redoMi.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: Selector(("cut:")), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: Selector(("copy:")), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: Selector(("paste:")), keyEquivalent: "v")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Select All", action: Selector(("selectAll:")), keyEquivalent: "a")

        let fmtItem = NSMenuItem(); main.addItem(fmtItem)
        let fmt = NSMenu(title: "Format"); fmtItem.submenu = fmt
        fmt.addItem(withTitle: "Bold", action: #selector(execBold), keyEquivalent: "b")
        fmt.addItem(withTitle: "Italic", action: #selector(execItalic), keyEquivalent: "i")
        fmt.addItem(withTitle: "Underline", action: #selector(execUnderline), keyEquivalent: "u")
        fmt.addItem(withTitle: "Strikethrough", action: #selector(execStrike), keyEquivalent: "")
        fmt.addItem(withTitle: "Highlight", action: #selector(execHighlight), keyEquivalent: "")
        fmt.addItem(.separator())
        for (t, blk) in [("Heading 1", "<h1>"), ("Heading 2", "<h2>"), ("Heading 3", "<h3>"), ("Body Text", "<p>")] {
            let m = fmt.addItem(withTitle: t, action: #selector(applyBlock(_:)), keyEquivalent: "")
            m.representedObject = blk
        }
        fmt.addItem(.separator())
        for (name, hex) in palette {
            let m = fmt.addItem(withTitle: "Color: \(name)", action: #selector(colorSelected(_:)), keyEquivalent: "")
            m.representedObject = hex
        }

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "View"); viewItem.submenu = view
        view.addItem(withTitle: "Reload", action: #selector(reloadDoc), keyEquivalent: "r")
        let modeMi = view.addItem(withTitle: "Toggle Read / Edit Mode", action: #selector(toggleMode), keyEquivalent: "e")
        modeMenuItem = modeMi
        let previewMi = view.addItem(withTitle: "Preview in Browser", action: #selector(previewInBrowser), keyEquivalent: "b")
        previewMi.keyEquivalentModifierMask = [.command, .control]

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "Window"); winItem.submenu = win
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "Zoom", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")

        let helpItem = NSMenuItem(); main.addItem(helpItem)
        let help = NSMenu(title: "Help"); helpItem.submenu = help
        help.addItem(withTitle: "How to use", action: #selector(showHelp), keyEquivalent: "")

        NSApp.mainMenu = main
    }

    // ---------- Toolbar ----------
    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.save, .saveAs, .flexibleSpace,
         .mode, .flexibleSpace,
         .format, .style, .color, .flexibleSpace,
         .status, .flexibleSpace, .browser]
    }
    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(toolbar)
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        switch id {
        case .save:
            return tb(id, symbol: "square.and.arrow.down", tip: "Save to original file (⌘S)", action: #selector(saveDoc))
        case .saveAs:
            return tb(id, symbol: "square.and.arrow.up", tip: "Save As (⇧⌘S)", action: #selector(saveAsDoc))
        case .mode:
            let it = NSToolbarItem(itemIdentifier: id)
            let bookImg = NSImage(systemSymbolName: "book", accessibilityDescription: "Read mode")
            let pencilImg = NSImage(systemSymbolName: "pencil", accessibilityDescription: "Edit mode")
            let seg = NSSegmentedControl(images: [bookImg, pencilImg].compactMap { $0 }, trackingMode: .selectOne,
                                         target: self, action: #selector(modeChanged(_:)))
            seg.segmentStyle = .texturedRounded
            seg.selectedSegment = 0
            seg.setWidth(38, forSegment: 0)
            seg.setWidth(38, forSegment: 1)
            seg.setToolTip("Read mode — clicks don't edit (⌘E switches)", forSegment: 0)
            seg.setToolTip("Edit mode — click text and type (⌘E switches)", forSegment: 1)
            modeSeg = seg
            it.view = seg
            it.label = "Mode"; it.paletteLabel = "Toggle Read / Edit Mode"
            return it
        case .format:
            let it = NSToolbarItem(itemIdentifier: id)
            let b = NSButton(frame: NSRect(x: 0, y: 0, width: 34, height: 28))
            b.bezelStyle = .texturedRounded
            if let img = NSImage(systemSymbolName: "textformat", accessibilityDescription: "Text formatting") {
                b.image = img
                b.imageScaling = .scaleProportionallyDown
            }
            b.target = self
            b.action = #selector(toggleFormat(_:))
            b.toolTip = "Text formatting — B / I / U / S"
            it.view = b
            it.label = "Format"; it.paletteLabel = "Text Formatting"
            return it
        case .style:
            let it = NSToolbarItem(itemIdentifier: id)
            let pop = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 86, height: 26))
            pop.pullsDown = true
            let titleItem = NSMenuItem(title: "Style", action: nil, keyEquivalent: "")
            titleItem.isEnabled = false
            pop.menu?.addItem(titleItem)
            for (t, blk) in [("Heading 1", "<h1>"), ("Heading 2", "<h2>"), ("Heading 3", "<h3>"), ("Body Text", "<p>")] {
                let m = NSMenuItem(title: t, action: #selector(applyBlock(_:)), keyEquivalent: "")
                m.representedObject = blk
                m.target = self
                pop.menu?.addItem(m)
            }
            pop.toolTip = "Paragraph style"
            it.view = pop
            it.label = "Style"; it.paletteLabel = "Paragraph Style"
            return it
        case .color:
            let it = NSToolbarItem(itemIdentifier: id)
            let pop = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 48, height: 26))
            pop.pullsDown = true
            let titleItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            titleItem.image = NSImage(systemSymbolName: "paintpalette", accessibilityDescription: "Font color")
            titleItem.isEnabled = false
            pop.menu?.addItem(titleItem)
            for (name, hex) in palette {
                let m = NSMenuItem(title: name, action: #selector(colorSelected(_:)), keyEquivalent: "")
                m.image = swatchImage(hex)
                m.representedObject = hex
                m.target = self
                pop.menu?.addItem(m)
            }
            pop.toolTip = "Font color"
            it.view = pop
            it.label = "Color"; it.paletteLabel = "Font Color"
            return it
        case .status:
            let it = NSToolbarItem(itemIdentifier: id)
            let label = NSTextField(labelWithString: "No file open")
            label.font = NSFont.systemFont(ofSize: 11, weight: .medium)
            label.textColor = .secondaryLabelColor
            statusLabel = label
            it.view = label
            it.label = "Save Status"; it.paletteLabel = "Save Status"
            return it
        case .browser:
            return tb(id, symbol: "safari", tip: "Save & preview in browser (⌃⌘B)", action: #selector(previewInBrowser))
        default:
            return nil
        }
    }

    private func tb(_ id: NSToolbarItem.Identifier, symbol: String? = nil, label: String? = nil,
                    tip: String, action: Selector, width: CGFloat = 34) -> NSToolbarItem {
        let it = NSToolbarItem(itemIdentifier: id)
        let b = NSButton(frame: NSRect(x: 0, y: 0, width: width, height: 28))
        b.bezelStyle = .texturedRounded
        if let s = symbol, let img = NSImage(systemSymbolName: s, accessibilityDescription: tip) {
            b.image = img
            b.imageScaling = .scaleProportionallyDown
        } else {
            b.title = label ?? tip
            b.font = NSFont.boldSystemFont(ofSize: 12)
        }
        b.target = self
        b.action = action
        b.toolTip = tip
        it.view = b
        it.label = tip; it.paletteLabel = tip
        return it
    }

    // ---------- Helpers ----------
    private func colorFromHex(_ hex: String) -> NSColor {
        var v: UInt64 = 0
        Scanner(string: String(hex.dropFirst())).scanHexInt64(&v)
        return NSColor(srgbRed: CGFloat((v >> 16) & 0xFF) / 255,
                       green: CGFloat((v >> 8) & 0xFF) / 255,
                       blue: CGFloat(v & 0xFF) / 255, alpha: 1)
    }

    private func swatchImage(_ hex: String) -> NSImage {
        let img = NSImage(size: NSSize(width: 12, height: 12))
        img.lockFocus()
        colorFromHex(hex).setFill()
        NSBezierPath(ovalIn: NSRect(x: 0, y: 0, width: 12, height: 12)).fill()
        img.unlockFocus()
        return img
    }
}

// MARK: - Toolbar identifiers
extension NSToolbarItem.Identifier {
    static let save = NSToolbarItem.Identifier("save")
    static let saveAs = NSToolbarItem.Identifier("saveAs")
    static let mode = NSToolbarItem.Identifier("mode")
    static let format = NSToolbarItem.Identifier("format")
    static let style = NSToolbarItem.Identifier("style")
    static let color = NSToolbarItem.Identifier("color")
    static let status = NSToolbarItem.Identifier("status")
    static let browser = NSToolbarItem.Identifier("browser")
}

// MARK: - Entry point
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
