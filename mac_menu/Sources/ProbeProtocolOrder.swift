import Foundation

struct ProbeProtocolRecommendation: Equatable {
    let supported: [String]
    let displayOrder: [String]

    var primary: String? {
        supported.first
    }
}

func inferredPreferredUpstreamApiMode(
    modelIdentifier: String,
    defaultMode: String
) -> String {
    let tokens = modelIdentifier
        .lowercased()
        .split { !$0.isLetter && !$0.isNumber }
    return tokens.contains("claude") ? "anthropic" : defaultMode
}

func probeProtocolPriority(
    modelIdentifier: String,
    defaultPriority: [String]
) -> [String] {
    guard let defaultMode = defaultPriority.first else { return [] }
    let preferred = inferredPreferredUpstreamApiMode(
        modelIdentifier: modelIdentifier,
        defaultMode: defaultMode
    )
    guard defaultPriority.contains(preferred) else { return defaultPriority }
    return [preferred] + defaultPriority.filter { $0 != preferred }
}

func probeProtocolRecommendation(
    priority: [String],
    availableModes: [String]
) -> ProbeProtocolRecommendation {
    let available = Set(availableModes)
    let supported = priority.first(where: { available.contains($0) }).map { [$0] } ?? []
    return ProbeProtocolRecommendation(
        supported: supported,
        displayOrder: supported
            + priority.filter { available.contains($0) && !supported.contains($0) }
            + priority.filter { !available.contains($0) }
    )
}
