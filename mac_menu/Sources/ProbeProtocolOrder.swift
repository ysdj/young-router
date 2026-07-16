import Foundation

struct ProbeProtocolRecommendation: Equatable {
    let supported: [String]
    let displayOrder: [String]

    var primary: String? {
        supported.first
    }
}

func probeProtocolRecommendation(
    priority: [String],
    availableModes: [String]
) -> ProbeProtocolRecommendation {
    let available = Set(availableModes)
    let supported = priority.filter { available.contains($0) }
    return ProbeProtocolRecommendation(
        supported: supported,
        displayOrder: supported + priority.filter { !available.contains($0) }
    )
}
