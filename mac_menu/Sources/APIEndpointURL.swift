import Foundation

private let recognizedAPIEndpointSuffixes: [[String]] = [
    ["v1", "chat", "completions"],
    ["v1", "chat", "completion"],
    ["v1", "images", "generations"],
    ["v1", "images", "generation"],
    ["v1", "completions"],
    ["v1", "completion"],
    ["v1", "complete"],
    ["v1", "responses"],
    ["v1", "response"],
    ["v1", "messages"],
    ["v1", "message"],
    ["v1", "models"],
    ["v1", "model"],
    ["v1", "chat"],
    ["v1", "images"],
    ["chat", "completions"],
    ["chat", "completion"],
    ["images", "generations"],
    ["images", "generation"],
    ["completions"],
    ["completion"],
    ["complete"],
    ["responses"],
    ["response"],
    ["messages"],
    ["message"],
    ["models"],
    ["model"],
    ["chat"],
    ["images"],
]

private func matchedAPIEndpointSuffix(_ parts: [String]) -> [String]? {
    for suffix in recognizedAPIEndpointSuffixes {
        guard parts.count >= suffix.count else { continue }
        let candidate = Array(parts.suffix(suffix.count)).map { $0.lowercased() }
        if candidate == suffix {
            return suffix
        }
    }
    return nil
}

private func endpointRootPath(_ rawPath: String) -> (path: String, versioned: Bool?) {
    var parts = rawPath
        .split(separator: "/", omittingEmptySubsequences: true)
        .map(String.init)
    guard let suffix = matchedAPIEndpointSuffix(parts) else {
        return (parts.joined(separator: "/"), nil)
    }
    parts.removeLast(suffix.count)
    return (parts.joined(separator: "/"), suffix.first == "v1")
}

private func endpointURLBaseString(_ components: URLComponents, path: String) -> String {
    var normalized = components
    normalized.percentEncodedPath = path.isEmpty ? "" : "/\(path)"
    return normalized.string?.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? ""
}

func apiEndpointURLCandidates(baseURL: String, endpoint: String) -> [URL] {
    let rawBase = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
    let endpointPath = endpoint.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    guard !rawBase.isEmpty, !endpointPath.isEmpty else { return [] }

    let urlString = rawBase.contains("://") ? rawBase : "https://\(rawBase)"
    guard var components = URLComponents(string: urlString),
          components.scheme?.isEmpty == false,
          components.host != nil else {
        return []
    }
    components.query = nil
    components.fragment = nil

    let endpointRoot = endpointRootPath(components.percentEncodedPath)
    let base = endpointURLBaseString(components, path: endpointRoot.path)
    guard !base.isEmpty else { return [] }

    let baseParts = endpointRoot.path
        .split(separator: "/", omittingEmptySubsequences: true)
        .map { $0.lowercased() }
    let paths: [String]
    if baseParts.last == "v1" {
        paths = [endpointPath]
    } else if endpointRoot.versioned == true {
        paths = ["v1/\(endpointPath)"]
    } else if endpointRoot.versioned == false {
        paths = [endpointPath, "v1/\(endpointPath)"]
    } else {
        paths = ["v1/\(endpointPath)", endpointPath]
    }

    var urls: [URL] = []
    var seen: Set<String> = []
    for path in paths {
        let candidate = "\(base)/\(path)"
        guard let url = URL(string: candidate), seen.insert(candidate).inserted else {
            continue
        }
        urls.append(url)
    }
    return urls
}
