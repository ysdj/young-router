import Cocoa
import Darwin
import Foundation

func emitJSON(_ object: Any) {
    do {
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    } catch {
        fputs("Could not encode harness output: \(error)\n", stderr)
        exit(2)
    }
}

func fail(_ message: String) -> Never {
    fputs("\(message)\n", stderr)
    exit(2)
}

func descendants(of view: NSView) -> [NSView] {
    [view] + view.subviews.flatMap { descendants(of: $0) }
}

func readPayload() -> RuntimeSettingsPayload {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    do {
        return try JSONDecoder().decode(RuntimeSettingsPayload.self, from: data)
    } catch {
        fail("Could not decode Runtime Settings payload: \(error)")
    }
}

func makeController(_ payload: RuntimeSettingsPayload) -> RuntimeSettingsDialogController {
    let controller = RuntimeSettingsDialogController(settings: payload.settings)
    controller.window.contentView?.layoutSubtreeIfNeeded()
    return controller
}

func layoutAudit(_ payload: RuntimeSettingsPayload, width: CGFloat) {
    let controller = makeController(payload)
    controller.window.setContentSize(NSSize(width: width, height: 620))
    guard let root = controller.window.contentView else {
        fail("Runtime Settings window has no content view.")
    }

    root.layoutSubtreeIfNeeded()
    guard let scrollView = descendants(of: root).compactMap({ $0 as? NSScrollView }).first,
          let documentView = scrollView.documentView,
          let formStack = documentView.subviews.compactMap({ $0 as? NSStackView }).first else {
        fail("Runtime Settings form hierarchy is missing its scroll, document, or stack view.")
    }

    scrollView.layoutSubtreeIfNeeded()
    documentView.layoutSubtreeIfNeeded()
    formStack.layoutSubtreeIfNeeded()
    root.layoutSubtreeIfNeeded()

    let stackRect = formStack.convert(formStack.bounds, to: documentView)
    var report: [String: Double] = [:]
    report["requested_width"] = Double(width)
    report["clip_width"] = Double(scrollView.contentView.bounds.width)
    report["clip_height"] = Double(scrollView.contentView.bounds.height)
    report["document_width"] = Double(documentView.bounds.width)
    report["document_height"] = Double(documentView.bounds.height)
    report["stack_width"] = Double(stackRect.width)
    report["stack_height"] = Double(stackRect.height)
    report["left_inset"] = Double(stackRect.minX)
    report["right_inset"] = Double(documentView.bounds.width - stackRect.maxX)
    report["top_inset"] = Double(stackRect.minY)
    report["bottom_inset"] = Double(documentView.bounds.height - stackRect.maxY)
    emitJSON(report)
}

func frameReport(_ view: NSView, in documentView: NSView) -> [String: Double] {
    let rect = view.convert(view.bounds, to: documentView)
    return [
        "x": Double(rect.minX),
        "y": Double(rect.minY),
        "width": Double(rect.width),
        "height": Double(rect.height),
        "min_x": Double(rect.minX),
        "max_x": Double(rect.maxX),
        "mid_y": Double(rect.midY),
    ]
}

func identifiedView(_ identifier: String, below root: NSView) -> NSView {
    guard let view = descendants(of: root).first(where: {
        $0.identifier?.rawValue == identifier
    }) else {
        fail("Runtime Settings view identifier is missing: \(identifier)")
    }
    return view
}

func alignmentAudit(_ payload: RuntimeSettingsPayload, width: CGFloat) {
    let controller = makeController(payload)
    controller.window.setContentSize(NSSize(width: width, height: 620))
    guard let root = controller.window.contentView else {
        fail("Runtime Settings window has no content view.")
    }

    root.layoutSubtreeIfNeeded()
    guard let scrollView = descendants(of: root).compactMap({ $0 as? NSScrollView }).first,
          let documentView = scrollView.documentView,
          let formStack = documentView.subviews.compactMap({ $0 as? NSStackView }).first else {
        fail("Runtime Settings form hierarchy is missing its scroll, document, or stack view.")
    }

    scrollView.layoutSubtreeIfNeeded()
    documentView.layoutSubtreeIfNeeded()
    formStack.layoutSubtreeIfNeeded()
    root.layoutSubtreeIfNeeded()

    let entries: [[String: Any]] = payload.settings.map { item in
        let prefix = item.key
        let row = identifiedView("RuntimeSettingsRow.\(prefix)", below: documentView)
        let inputRow = identifiedView("RuntimeSettingsInputRow.\(prefix)", below: row)
        let label = identifiedView("RuntimeSettingsLabel.\(prefix)", below: inputRow)
        let valueSlot = identifiedView("RuntimeSettingsValueSlot.\(prefix)", below: inputRow)
        let actionSlot = identifiedView("RuntimeSettingsActionSlot.\(prefix)", below: inputRow)
        let unit = identifiedView("RuntimeSettingsUnit.\(prefix)", below: inputRow)
        let help = identifiedView("RuntimeSettingsHelp.\(prefix)", below: row)
        guard let control = controller.fields[item.key] else {
            fail("Runtime Settings control is missing for: \(item.key)")
        }

        return [
            "key": item.key,
            "row": frameReport(row, in: documentView),
            "input_row": frameReport(inputRow, in: documentView),
            "label": frameReport(label, in: documentView),
            "value_slot": frameReport(valueSlot, in: documentView),
            "action_slot": frameReport(actionSlot, in: documentView),
            "unit": frameReport(unit, in: documentView),
            "help": frameReport(help, in: documentView),
            "control": frameReport(control, in: documentView),
        ]
    }

    emitJSON([
        "requested_width": Double(width),
        "document": frameReport(documentView, in: documentView),
        "form_stack": frameReport(formStack, in: documentView),
        "entries": entries,
    ])
}

func controlsAudit(_ payload: RuntimeSettingsPayload) {
    let controller = makeController(payload)
    let entries: [[String: Any]] = payload.settings.map { item in
        let control = controller.fields[item.key]
        let hasVisibleText = (control as? NSTextField)
            .map { !$0.stringValue.isEmpty } ?? false
        return [
            "key": item.key,
            "label": item.label,
            "class_name": control.map { String(describing: type(of: $0)) } ?? "",
            "accessibility_label": control?.accessibilityLabel() ?? "",
            "is_secure": control is NSSecureTextField,
            "has_visible_text": hasVisibleText,
        ]
    }
    emitJSON([
        "settings_count": payload.settings.count,
        "fields_count": controller.fields.count,
        "entries": entries,
    ])
}

func secretAudit(_ payload: RuntimeSettingsPayload) {
    guard let item = payload.settings.first(where: {
        $0.key == "LITELLM_MENU_VISION_BRIDGE_API_KEY"
    }) else {
        fail("Runtime Settings payload is missing the Vision Bridge API key.")
    }
    let controller = makeController(payload)
    guard let field = controller.fields[item.key] as? NSSecureTextField else {
        fail("Vision Bridge API key does not use NSSecureTextField.")
    }

    let untouched = controller.currentValues()[item.key] ?? ""
    let initialDisplay = field.stringValue
    let initialPlaceholder = field.placeholderString ?? ""

    controller.setValue("synthetic-replacement", for: item)
    let replacement = controller.currentValues()[item.key] ?? ""

    controller.setValue("", for: item, explicitlyClearingSensitiveValue: true)
    let cleared = controller.currentValues()[item.key] ?? ""

    emitJSON([
        "initial_display": initialDisplay,
        "initial_placeholder": initialPlaceholder,
        "untouched_value": untouched,
        "replacement_value": replacement,
        "cleared_value": cleared,
    ])
}

func validationAudit(_ payload: RuntimeSettingsPayload, encodedValues: String) {
    guard let data = Data(base64Encoded: encodedValues),
          let values = try? JSONDecoder().decode([String: String].self, from: data) else {
        fail("Validation values are not valid base64-encoded JSON.")
    }
    let controller = makeController(payload)
    for (key, value) in values {
        guard let item = payload.settings.first(where: { $0.key == key }) else {
            fail("Unknown Runtime Settings key: \(key)")
        }
        controller.setValue(value, for: item)
    }
    let message = controller.validationMessage()
    emitJSON([
        "valid": message == nil,
        "message": message ?? "",
    ])
}

let arguments = CommandLine.arguments
guard arguments.count >= 2 else {
    fail("usage: runtime-settings-dialog-harness {layout WIDTH|alignment WIDTH|controls|secret|validate BASE64_JSON_VALUES}")
}

let payload = readPayload()
switch arguments[1] {
case "layout":
    guard arguments.count == 3, let width = Double(arguments[2]), width >= 760 else {
        fail("layout requires a window width of at least 760.")
    }
    layoutAudit(payload, width: CGFloat(width))
case "alignment":
    guard arguments.count == 3, let width = Double(arguments[2]), width >= 760 else {
        fail("alignment requires a window width of at least 760.")
    }
    alignmentAudit(payload, width: CGFloat(width))
case "controls":
    guard arguments.count == 2 else {
        fail("controls does not accept additional arguments.")
    }
    controlsAudit(payload)
case "secret":
    guard arguments.count == 2 else {
        fail("secret does not accept additional arguments.")
    }
    secretAudit(payload)
case "validate":
    guard arguments.count == 3 else {
        fail("validate requires base64-encoded JSON setting values.")
    }
    validationAudit(payload, encodedValues: arguments[2])
default:
    fail("Unknown harness command: \(arguments[1])")
}
