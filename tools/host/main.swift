import Cocoa
import WebKit
import UniformTypeIdentifiers

// MARK: - 支持拖放的容器视图
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

// MARK: - 主控制器
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate,
                         WKScriptMessageHandler, NSWindowDelegate, NSToolbarDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var statusLabel: NSTextField?
    var modeSeg: NSSegmentedControl?
    var modeMenuItem: NSMenuItem?
    var currentURL: URL?
    var pendingURL: URL?
    var closeAfterSave = false
    var editMode = false
    var isDirty = false { didSet { refreshChrome() } }

    let palette: [(String, String)] = [
        ("白", "#FFFFFF"), ("黄", "#FFD166"), ("橙", "#FF9F43"), ("红", "#FF6B6B"),
        ("绿", "#51CF66"), ("蓝", "#4DABF7"), ("紫", "#B197FC"), ("灰", "#ADB5BD"), ("黑", "#343A40")
    ]

    // ---------- 注入页面的编辑脚本 ----------
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

    // ---------- 启动 ----------
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

        // 窗口定位：鼠标所在屏居中（多显示器可靠）
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

    private func argURL() -> URL? {
        let fm = FileManager.default
        for a in ProcessInfo.processInfo.arguments.dropFirst() {
            if a.hasPrefix("-") { continue }
            let u = URL(fileURLWithPath: a)
            if fm.fileExists(atPath: u.path), ["html", "htm"].contains(u.pathExtension.lowercased()) { return u }
        }
        return nil
    }

    // ---------- 打开 / 保存 ----------
    func loadFile(_ url: URL) {
        let fm = FileManager.default
        guard fm.fileExists(atPath: url.path) else { warn("找不到文件：\(url.path)"); return }
        currentURL = url
        isDirty = false
        editMode = false
        closeAfterSave = false
        // 允许读取用户主目录下的相对资源（图片/CSS）；主目录外则退回文件所在目录
        let home = URL(fileURLWithPath: NSHomeDirectory())
        let access = url.standardizedFileURL.path.hasPrefix(home.path) ? home : url.deletingLastPathComponent()
        webView.loadFileURL(url, allowingReadAccessTo: access)
        refreshChrome()
    }

    @objc func openPanel(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [UTType.html]
        panel.message = "选择要编辑的 HTML 文件"
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
                } catch { self.warn("保存失败：\(error.localizedDescription)") }
            } else {
                self.warn("保存失败：无法读取编辑内容 \(err?.localizedDescription ?? "")")
            }
        }
    }

    @objc func saveAsDoc() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [UTType.html]
        panel.nameFieldStringValue = currentURL?.lastPathComponent ?? "未命名.html"
        panel.directoryURL = currentURL?.deletingLastPathComponent()
        panel.beginSheetModal(for: window) { [weak self] resp in
            guard let self, resp == .OK, let url = panel.url else { return }
            self.currentURL = url
            self.performSave(completion: nil)
        }
    }

    @objc func previewInBrowser() {
        guard let u = currentURL else { warn("先打开一个文件"); return }
        if isDirty { performSave { NSWorkspace.shared.open(u) } }
        else { NSWorkspace.shared.open(u) }
    }

    // ---------- 阅读模式 / 编辑模式 ----------
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

    @objc func reloadDoc() {
        guard currentURL != nil else { return }
        if isDirty {
            let a = NSAlert()
            a.messageText = "有未保存的修改，重新加载会丢弃这些修改？"
            a.addButton(withTitle: "重新加载")
            a.addButton(withTitle: "取消")
            a.beginSheetModal(for: window) { [weak self] r in
                if r == .alertFirstButtonReturn { self?.editMode = false; self?.webView.reload() }
            }
        } else { editMode = false; webView.reload() }
    }

    private func showWelcome() {
        let html = """
        <!DOCTYPE html><html><head><meta charset="utf-8"><style>
        body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#0d1117;color:#e6edf3;font-family:-apple-system,"PingFang SC",sans-serif}
        .box{text-align:center;max-width:600px;line-height:2.1}
        h1{font-size:26px;margin-bottom:12px}
        p{color:#8b949e;font-size:15px;margin:6px 0}
        kbd{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:2px 8px;font-family:Menlo,monospace;font-size:13px;color:#79c0ff}
        </style></head><body><div class="box">
        <h1>Pree HTML Station</h1>
        <p>把 <b>.html</b> 文件拖进这个窗口，或按 <kbd>⌘O</kbd> 打开文件</p>
        <p>打开后默认<b>阅读模式</b>，点击不会误改文字 · 按 <kbd>⌘E</kbd> 进入编辑，像改 Word 一样 · <kbd>⌘S</kbd> 保存回原文件</p>
        </div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // ---------- 编辑命令 ----------
    @objc func execBold()        { exec("bold");        isDirty = true }
    @objc func execItalic()      { exec("italic");      isDirty = true }
    @objc func execUnderline()   { exec("underline");   isDirty = true }
    @objc func execStrike()      { exec("strikeThrough"); isDirty = true }
    @objc func blockH1()         { exec("formatBlock", "<h1>"); isDirty = true }
    @objc func blockH2()         { exec("formatBlock", "<h2>"); isDirty = true }
    @objc func blockH3()         { exec("formatBlock", "<h3>"); isDirty = true }
    @objc func blockPara()       { exec("formatBlock", "<p>");  isDirty = true }

    @objc func applyBlock(_ sender: NSMenuItem) {
        if let blk = sender.representedObject as? String { exec("formatBlock", blk); isDirty = true }
    }

    @objc func colorSelected(_ sender: Any?) {
        var hex: String?
        if let mi = sender as? NSMenuItem { hex = mi.representedObject as? String }
        else if let pop = sender as? NSPopUpButton, pop.indexOfSelectedItem >= 0,
                pop.indexOfSelectedItem < palette.count {
            hex = palette[pop.indexOfSelectedItem].1
            pop.selectItem(at: -1)
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
        a.messageText = "使用说明"
        a.informativeText = """
        1. ⌘O 打开任意 .html 文件（或把文件拖进窗口）
        2. 默认阅读模式：点击不会改到文字，链接可以直接点
        3. 按 ⌘E 或点工具栏「编辑」进入编辑模式，
           点选文字直接修改，像改 Word 文档一样
        4. ⌘S 保存回原文件 · ⇧⌘S 另存为
        5. 工具栏最右侧按钮：自动保存后在浏览器中预览
        """
        a.runModal()
    }

    private func warn(_ msg: String) {
        let a = NSAlert()
        a.messageText = msg
        a.runModal()
    }

    // ---------- 状态显示 ----------
    private func refreshChrome() {
        guard window != nil else { return }
        if let url = currentURL {
            window.title = url.lastPathComponent + (isDirty ? " •" : "") + (editMode ? "（编辑）" : "")
        } else {
            window.title = "Pree HTML Station"
        }
        modeSeg?.selectedSegment = editMode ? 1 : 0
        modeMenuItem?.state = editMode ? .on : .off
        if let label = statusLabel {
            if currentURL == nil {
                label.stringValue = "未打开文件"
                label.textColor = .secondaryLabelColor
            } else if isDirty {
                label.stringValue = "● 未保存"
                label.textColor = .systemOrange
            } else {
                label.stringValue = "✓ 已保存"
                label.textColor = .systemGreen
            }
        }
    }

    // ---------- JS 消息 ----------
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        if (message.body as? String) == "dirty" { isDirty = true }
    }

    // ---------- 导航拦截 ----------
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard action.navigationType == .linkActivated, let url = action.request.url else {
            decisionHandler(.allow); return
        }
        // 同文件锚点（目录跳转）：原生平滑滚动
        if url.isFileURL, url.standardizedFileURL == currentURL?.standardizedFileURL {
            decisionHandler(.allow); return
        }
        // 站内 .html 互链：直接在本窗口打开（阅读模式下的文档浏览）
        if url.isFileURL, ["html", "htm"].contains(url.pathExtension.lowercased()) {
            if isDirty {
                let a = NSAlert()
                a.messageText = "有未保存的修改，打开新文件前先保存？"
                a.addButton(withTitle: "保存并打开")
                a.addButton(withTitle: "不保存")
                a.addButton(withTitle: "取消")
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
        // 其他链接（http/图片/PDF 等）：交给系统默认应用
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    // ---------- 关闭保护 ----------
    func windowShouldClose(_ s: NSWindow) -> Bool {
        guard isDirty else { return true }
        let a = NSAlert()
        a.messageText = "有未保存的修改"
        a.informativeText = currentURL?.lastPathComponent ?? ""
        a.addButton(withTitle: "保存")
        a.addButton(withTitle: "不保存")
        a.addButton(withTitle: "取消")
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

    // ---------- 菜单 ----------
    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于 Pree HTML Station",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let fileItem = NSMenuItem(); main.addItem(fileItem)
        let file = NSMenu(title: "文件"); fileItem.submenu = file
        file.addItem(withTitle: "打开…", action: #selector(openPanel(_:)), keyEquivalent: "o")
        file.addItem(.separator())
        file.addItem(withTitle: "保存", action: #selector(saveDoc), keyEquivalent: "s")
        let saveAsMi = file.addItem(withTitle: "另存为…", action: #selector(saveAsDoc), keyEquivalent: "s")
        saveAsMi.keyEquivalentModifierMask = [.command, .shift]
        file.addItem(.separator())
        file.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")

        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "编辑"); editItem.submenu = edit
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redoMi = edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redoMi.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: Selector(("cut:")), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: Selector(("copy:")), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: Selector(("paste:")), keyEquivalent: "v")
        edit.addItem(.separator())
        edit.addItem(withTitle: "全选", action: Selector(("selectAll:")), keyEquivalent: "a")

        let fmtItem = NSMenuItem(); main.addItem(fmtItem)
        let fmt = NSMenu(title: "格式"); fmtItem.submenu = fmt
        fmt.addItem(withTitle: "加粗", action: #selector(execBold), keyEquivalent: "b")
        fmt.addItem(withTitle: "斜体", action: #selector(execItalic), keyEquivalent: "i")
        fmt.addItem(withTitle: "下划线", action: #selector(execUnderline), keyEquivalent: "u")
        fmt.addItem(withTitle: "删除线", action: #selector(execStrike), keyEquivalent: "")
        fmt.addItem(.separator())
        for (t, blk) in [("标题 1", "<h1>"), ("标题 2", "<h2>"), ("标题 3", "<h3>"), ("正文", "<p>")] {
            let m = fmt.addItem(withTitle: t, action: #selector(applyBlock(_:)), keyEquivalent: "")
            m.representedObject = blk
        }
        fmt.addItem(.separator())
        for (name, hex) in palette {
            let m = fmt.addItem(withTitle: "颜色：\(name)", action: #selector(colorSelected(_:)), keyEquivalent: "")
            m.representedObject = hex
        }

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "视图"); viewItem.submenu = view
        view.addItem(withTitle: "重新加载", action: #selector(reloadDoc), keyEquivalent: "r")
        let modeMi = view.addItem(withTitle: "阅读 / 编辑模式", action: #selector(toggleMode), keyEquivalent: "e")
        modeMenuItem = modeMi
        let previewMi = view.addItem(withTitle: "在浏览器中预览", action: #selector(previewInBrowser), keyEquivalent: "b")
        previewMi.keyEquivalentModifierMask = [.command, .control]

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "窗口"); winItem.submenu = win
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")

        let helpItem = NSMenuItem(); main.addItem(helpItem)
        let help = NSMenu(title: "帮助"); helpItem.submenu = help
        help.addItem(withTitle: "使用说明", action: #selector(showHelp), keyEquivalent: "")

        NSApp.mainMenu = main
    }

    // ---------- 工具栏 ----------
    func toolbarDefaultItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        [.open, .save, .saveAs, .flexibleSpace,
         .mode, .flexibleSpace,
         .bold, .italic, .underline, .strike, .flexibleSpace,
         .h1, .h2, .h3, .para, .color, .flexibleSpace,
         .status, .flexibleSpace, .browser]
    }
    func toolbarAllowedItemIdentifiers(_ toolbar: NSToolbar) -> [NSToolbarItem.Identifier] {
        toolbarDefaultItemIdentifiers(toolbar)
    }

    func toolbar(_ toolbar: NSToolbar, itemForItemIdentifier id: NSToolbarItem.Identifier,
                 willBeInsertedIntoToolbar flag: Bool) -> NSToolbarItem? {
        switch id {
        case .open:
            return tb(id, symbol: "folder", tip: "打开（⌘O）", action: #selector(openPanel(_:)))
        case .save:
            return tb(id, symbol: "square.and.arrow.down", tip: "保存到原文件（⌘S）", action: #selector(saveDoc))
        case .saveAs:
            return tb(id, symbol: "square.and.arrow.down.on.square", tip: "另存为（⇧⌘S）", action: #selector(saveAsDoc))
        case .mode:
            let it = NSToolbarItem(itemIdentifier: id)
            let seg = NSSegmentedControl(labels: ["阅读", "编辑"], trackingMode: .selectOne,
                                         target: self, action: #selector(modeChanged(_:)))
            seg.segmentStyle = .texturedRounded
            seg.selectedSegment = 0
            seg.toolTip = "阅读模式：点击不进入编辑（⌘E 切换）"
            modeSeg = seg
            it.view = seg
            it.label = "模式"; it.paletteLabel = "阅读 / 编辑模式"
            return it
        case .bold:
            return tb(id, symbol: "bold", tip: "加粗（⌘B）", action: #selector(execBold))
        case .italic:
            return tb(id, symbol: "italic", tip: "斜体（⌘I）", action: #selector(execItalic))
        case .underline:
            return tb(id, symbol: "underline", tip: "下划线（⌘U）", action: #selector(execUnderline))
        case .strike:
            return tb(id, symbol: "strikethrough", tip: "删除线", action: #selector(execStrike))
        case .h1:
            return tb(id, label: "H1", tip: "标题 1", action: #selector(blockH1))
        case .h2:
            return tb(id, label: "H2", tip: "标题 2", action: #selector(blockH2))
        case .h3:
            return tb(id, label: "H3", tip: "标题 3", action: #selector(blockH3))
        case .para:
            return tb(id, label: "正文", tip: "正文段落", action: #selector(blockPara))
        case .color:
            let it = NSToolbarItem(itemIdentifier: id)
            let pop = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 96, height: 26))
            pop.addItems(withTitles: palette.map(\.0))
            for (i, (_, hex)) in palette.enumerated() {
                pop.item(at: i)?.image = swatchImage(hex)
            }
            pop.target = self
            pop.action = #selector(colorSelected(_:))
            pop.toolTip = "字体颜色"
            pop.selectItem(at: -1)
            it.view = pop
            it.label = "颜色"; it.paletteLabel = "字体颜色"
            return it
        case .status:
            let it = NSToolbarItem(itemIdentifier: id)
            let label = NSTextField(labelWithString: "未打开文件")
            label.font = NSFont.systemFont(ofSize: 11, weight: .medium)
            label.textColor = .secondaryLabelColor
            statusLabel = label
            it.view = label
            it.label = "保存状态"; it.paletteLabel = "保存状态"
            return it
        case .browser:
            return tb(id, symbol: "safari", tip: "保存并在浏览器中预览（⌃⌘B）", action: #selector(previewInBrowser))
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

    // ---------- 小工具 ----------
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

// MARK: - 工具栏标识符
extension NSToolbarItem.Identifier {
    static let open = NSToolbarItem.Identifier("open")
    static let save = NSToolbarItem.Identifier("save")
    static let saveAs = NSToolbarItem.Identifier("saveAs")
    static let mode = NSToolbarItem.Identifier("mode")
    static let bold = NSToolbarItem.Identifier("bold")
    static let italic = NSToolbarItem.Identifier("italic")
    static let underline = NSToolbarItem.Identifier("underline")
    static let strike = NSToolbarItem.Identifier("strike")
    static let h1 = NSToolbarItem.Identifier("h1")
    static let h2 = NSToolbarItem.Identifier("h2")
    static let h3 = NSToolbarItem.Identifier("h3")
    static let para = NSToolbarItem.Identifier("para")
    static let color = NSToolbarItem.Identifier("color")
    static let status = NSToolbarItem.Identifier("status")
    static let browser = NSToolbarItem.Identifier("browser")
}

// MARK: - 入口
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
