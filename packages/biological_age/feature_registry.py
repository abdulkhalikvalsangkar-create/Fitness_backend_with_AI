from dataclasses import dataclass
from typing import Callable, List, Dict, Any, Optional

@dataclass
class FeatureDefinition:
    name: str
    required_columns: List[str]
    calculator: Callable
    requires_timestamp: bool = False
    is_time_series: bool = False
    min_valid_ratio: float = 0.70
    description: str = ""

class FeatureRegistry:
    def __init__(self):
        self.features: Dict[str, FeatureDefinition] = {}

    def register(self, feature: FeatureDefinition):
        self.features[feature.name] = feature

    def get_all(self) -> Dict[str, FeatureDefinition]:
        return self.features

    def available_features(self, columns: List[str]) -> Dict[str, FeatureDefinition]:
        cols_lower = {str(c).strip().lower(): c for c in columns}
        available = {}
        for name, feature in self.features.items():
            # Check if all required columns (case-insensitive) are present
            if all(any(req.lower() == c.lower() for c in columns) for req in feature.required_columns):
                available[name] = feature
        return available
