import Cocoa

extension AppDelegate {
    func writeCommandToVisualFile(action: String, filename: String, pageTitle: String, title: String) {
        writeCommandToTempFile(
            action: action,
            filename: filename,
            title: title,
            transform: { self.visualLogHTML(title: pageTitle, body: $0) }
        )
    }

    func writeCommandToTempFile(
        action: String,
        filename: String,
        title: String,
        transform: @escaping (String) -> String = { $0 }
    ) {
        statusMenuItem.title = "Status: Opening \(filename)"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.control(action)
            let stampedFilename = self.timestampedTempFilename(filename)
            let fileURL = FileManager.default.temporaryDirectory.appendingPathComponent(stampedFilename)

            DispatchQueue.main.async {
                self.updateStatus()
                if result.0 == 0, let data = transform(result.1).data(using: .utf8) {
                    do {
                        try data.write(to: fileURL, options: .atomic)
                        NSWorkspace.shared.open(fileURL)
                    } catch {
                        self.showAlert(title: title, message: String(describing: error))
                    }
                } else {
                    self.showAlert(title: title, message: result.1)
                }
            }
        }
    }

    func visualLogHTML(title: String, body: String) -> String {
        let output = body.isEmpty ? "No output." : body
        let lines = output.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let generatedAtFormatter = DateFormatter()
        generatedAtFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        let generatedAt = generatedAtFormatter.string(from: Date())
        let lineLevels = lines.map { logLineLevel($0) }
        let errorCount = lineLevels.filter { $0 == "error" }.count
        let warningCount = lineLevels.filter { $0 == "warning" }.count
        let linesHTML = zip(lines.indices, lines).map { index, line in
            let level = lineLevels[index]
            let escapedLine = htmlEscape(line.isEmpty ? " " : line)
            let searchText = htmlEscape(line.lowercased())
            return """
              <div class="log-line level-\(level)" data-text="\(searchText)">
                <span class="line-number">\(index + 1)</span>
                <code>\(escapedLine)</code>
              </div>
            """
        }.joined(separator: "\n")

        let escapedTitle = htmlEscape(title)
        let escapedGeneratedAt = htmlEscape(generatedAt)
        let byteCount = output.lengthOfBytes(using: .utf8)
        return """
        <!doctype html>
        <html lang="en">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="icon" href="data:,">
        <title>\(escapedTitle)</title>
        <style>
        :root {
          --bg: #f6f7f9;
          --panel: #ffffff;
          --line: #d9dee7;
          --text: #172033;
          --muted: #637083;
          --soft: #eef1f5;
          --green: #1f8a5b;
          --blue: #246fc7;
          --amber: #b56a00;
          --red: #b42318;
          --violet: #6b4bb8;
        }
        * { box-sizing: border-box; }
        body {
          margin: 0;
          background: var(--bg);
          color: var(--text);
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          font-size: 14px;
          line-height: 1.45;
        }
        header {
          position: sticky;
          top: 0;
          z-index: 10;
          background: rgba(246, 247, 249, 0.96);
          border-bottom: 1px solid var(--line);
          padding: 18px 22px 14px;
        }
        h1 {
          margin: 0 0 4px;
          font-size: 22px;
          letter-spacing: 0;
        }
        .sub {
          color: var(--muted);
          font-size: 13px;
        }
        .toolbar {
          display: flex;
          gap: 10px;
          align-items: center;
          margin-top: 14px;
          flex-wrap: wrap;
        }
        input[type="search"] {
          width: min(520px, 100%);
          padding: 9px 10px;
          border: 1px solid var(--line);
          border-radius: 6px;
          background: #fff;
          color: var(--text);
        }
        button {
          border: 1px solid var(--line);
          background: #fff;
          color: var(--text);
          border-radius: 6px;
          padding: 8px 10px;
          cursor: pointer;
        }
        button.active {
          background: #172033;
          color: #fff;
          border-color: #172033;
        }
        main {
          padding: 18px 22px 34px;
        }
        .stats {
          display: grid;
          grid-template-columns: repeat(4, minmax(140px, 1fr));
          gap: 10px;
          margin-bottom: 16px;
        }
        .metric {
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 12px;
        }
        .metric b {
          display: block;
          font-size: 24px;
        }
        .metric span {
          color: var(--muted);
          font-size: 12px;
          text-transform: uppercase;
        }
        .stat-row {
          display: flex;
          gap: 6px;
          flex-wrap: wrap;
          margin: 10px 0 16px;
        }
        .stat-chip {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          border: 1px solid var(--line);
          border-radius: 999px;
          background: #fff;
          padding: 3px 8px;
          color: var(--muted);
          font-size: 12px;
          white-space: nowrap;
        }
        .log-card {
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 8px;
          overflow: hidden;
        }
        .log-card-header {
          display: flex;
          justify-content: space-between;
          gap: 12px;
          align-items: center;
          padding: 12px 14px;
          border-bottom: 1px solid var(--line);
          background: #fbfcfe;
          color: var(--muted);
          font-size: 12px;
        }
        .log-lines {
          background: #fff;
          font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
          font-size: 12px;
          line-height: 1.45;
        }
        .log-line {
          display: grid;
          grid-template-columns: 58px minmax(0, 1fr);
          margin: 0;
          border-bottom: 1px solid var(--soft);
        }
        .log-line:last-child {
          border-bottom: 0;
        }
        .line-number {
          color: var(--muted);
          background: #f8fafc;
          border-right: 1px solid var(--soft);
          padding: 5px 10px;
          text-align: right;
          user-select: none;
        }
        .log-line code {
          display: block;
          padding: 5px 10px;
          color: var(--text);
          white-space: pre-wrap;
          overflow-wrap: anywhere;
        }
        .level-warning .line-number {
          color: var(--amber);
          background: #fff7e8;
        }
        .level-error .line-number {
          color: var(--red);
          background: #fff0ee;
        }
        .level-success .line-number {
          color: var(--green);
          background: #eaf7f0;
        }
        .empty {
          color: var(--muted);
          padding: 18px;
        }
        @media (max-width: 760px) {
          .stats { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
          .log-line { grid-template-columns: 46px minmax(0, 1fr); }
        }
        </style>
        </head>
        <body>
        <header>
          <h1>\(escapedTitle)</h1>
          <div class="sub">Generated \(escapedGeneratedAt). Showing \(lines.count) lines from LiteLLM Menu.</div>
          <div class="toolbar">
            <input id="search" type="search" placeholder="Search this log">
            <button data-filter="all" class="active">All</button>
            <button data-filter="warning">Warnings</button>
            <button data-filter="error">Errors</button>
          </div>
        </header>
        <main>
          <section class="stats">
            <div class="metric"><b>\(lines.count)</b><span>lines</span></div>
            <div class="metric"><b>\(byteCount)</b><span>bytes</span></div>
            <div class="metric"><b>\(warningCount)</b><span>warnings</span></div>
            <div class="metric"><b>\(errorCount)</b><span>errors</span></div>
          </section>
          <div class="stat-row">
            <span class="stat-chip"><span id="visible-count">\(lines.count)</span> visible lines</span>
          </div>
          <section class="log-card">
            <div class="log-card-header">
              <span>\(escapedTitle)</span>
              <span>\(lines.count) lines</span>
            </div>
            <div id="log-lines" class="log-lines">
        \(linesHTML)
            </div>
          </section>
        </main>
        <script>
        const lines = Array.from(document.querySelectorAll('.log-line'));
        const search = document.getElementById('search');
        const buttons = Array.from(document.querySelectorAll('button[data-filter]'));
        const visibleCount = document.getElementById('visible-count');
        let activeFilter = 'all';

        function applyFilters() {
          const term = search.value.trim().toLowerCase();
          let shown = 0;
          for (const line of lines) {
            const matchesSearch = !term || line.dataset.text.includes(term);
            const matchesFilter = activeFilter === 'all' || line.classList.contains(`level-${activeFilter}`);
            const visible = matchesSearch && matchesFilter;
            line.hidden = !visible;
            if (visible) shown += 1;
          }
          visibleCount.textContent = shown;
        }

        search.addEventListener('input', applyFilters);
        for (const button of buttons) {
          button.addEventListener('click', () => {
            activeFilter = button.dataset.filter;
            for (const item of buttons) item.classList.toggle('active', item === button);
            applyFilters();
          });
        }
        </script>
        </body>
        </html>
        """
    }

    func logLineLevel(_ line: String) -> String {
        let lowercased = line.lowercased()
        if lowercased.contains("error")
            || lowercased.contains("failed")
            || lowercased.contains("exception")
            || lowercased.contains("traceback")
            || lowercased.contains("unhealthy") {
            return "error"
        }
        if lowercased.contains("warn")
            || lowercased.contains("retry")
            || lowercased.contains("fallback")
            || lowercased.contains("missing") {
            return "warning"
        }
        if lowercased.contains("success")
            || lowercased.contains("started")
            || lowercased.contains("running")
            || lowercased.contains("valid") {
            return "success"
        }
        return "neutral"
    }

    func htmlEscape(_ value: String) -> String {
        var escaped = ""
        escaped.reserveCapacity(value.count)
        for character in value {
            switch character {
            case "&":
                escaped += "&amp;"
            case "<":
                escaped += "&lt;"
            case ">":
                escaped += "&gt;"
            case "\"":
                escaped += "&quot;"
            case "'":
                escaped += "&#39;"
            default:
                escaped.append(character)
            }
        }
        return escaped
    }

    func timestampedTempFilename(_ filename: String) -> String {
        let url = URL(fileURLWithPath: filename)
        let name = url.deletingPathExtension().lastPathComponent
        let ext = url.pathExtension
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        let stamp = formatter.string(from: Date())
        if ext.isEmpty {
            return "\(name)-\(stamp)"
        }
        return "\(name)-\(stamp).\(ext)"
    }

    func showAlert(title: String, message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = shortAlertMessage(message)
        alert.alertStyle = .warning
        alert.runModal()
    }
}
