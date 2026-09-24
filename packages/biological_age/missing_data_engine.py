from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
import pandas as pd
from packages.biological_age.schema_detector import SchemaReport

# Standard core features expected in full assessment
CORE_EXPECTED_PARAMETERS: List[str] = [
    "sleep",
    "activity",
    "resting_heart_rate",
    "hrv",
    "respiratory_rate",
    "recovery_score",
    "vo2_max"
]

@dataclass
class MissingDataReport:
    available_features: Dict[str, float] = field(default_factory=dict)
    unavailable_features: List[Dict[str, str]] = field(default_factory=list)
    additional_input_detected: List[Dict[str, str]] = field(default_factory=list)
    data_quality_percent: float = 100.0
    quality_breakdown: Dict[str, Any] = field(default_factory=dict)


class MissingDataEngine:
    """
    Evaluates schema detection and feature validation outputs:
    1. Records which features are validated and available.
    2. Identifies expected clinical parameters that are unavailable (with reasons).
    3. Identifies new/unknown parameters that appear (Case 4: flagged and ignored).
    4. Computes overall data quality score (% valid data across processed signals).
    """

    def evaluate(
        self,
        schema_report: SchemaReport,
        extracted_features: Dict[str, float],
        feature_quality: Dict[str, Dict[str, Any]],
        failed_features: Dict[str, str]
    ) -> MissingDataReport:
        report = MissingDataReport()
        report.available_features = dict(extracted_features)
        report.quality_breakdown = feature_quality

        # 1. Track unavailable features from calculation failures / data quality failures
        for name, reason in failed_features.items():
            report.unavailable_features.append({
                "name": name,
                "reason": reason
            })

        # 2. Check core expected clinical parameters that weren't even in columns
        feature_keys_lower = {k.lower() for k in extracted_features.keys()}
        failed_keys_lower = {k.lower() for k in failed_features.keys()}

        for core_param in CORE_EXPECTED_PARAMETERS:
            # Check synonyms / prefixes
            matches = any(
                core_param in f or f in core_param
                for f in (feature_keys_lower | failed_keys_lower)
            )
            if not matches:
                # Clean label
                display_name = core_param.replace("_", " ").title()
                report.unavailable_features.append({
                    "name": display_name,
                    "reason": "Parameter not present in uploaded dataset."
                })

        # 3. Track additional / unknown inputs detected (Case 4)
        for unk in schema_report.unknown_columns:
            report.additional_input_detected.append({
                "column": unk,
                "reason": "Not used (no validated model mapping)"
            })

        # 4. Compute overall data quality percentage
        if feature_quality:
            usable_count = 0
            valid_ratios = []
            for col, q in feature_quality.items():
                ratio = q.get("valid_ratio", 0.0)
                valid_ratios.append(ratio)
                if q.get("usable", False):
                    usable_count += 1
            avg_ratio = sum(valid_ratios) / len(valid_ratios) if valid_ratios else 1.0
            report.data_quality_percent = round(avg_ratio * 100.0, 1)
        else:
            report.data_quality_percent = 100.0 if extracted_features else 0.0

        return report
