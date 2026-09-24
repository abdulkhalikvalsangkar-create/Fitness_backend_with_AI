import pandas as pd

class DataQualityChecker:
    def __init__(self, min_valid_ratio=0.70):
        self.min_valid_ratio = min_valid_ratio

    def check_column(self, df, column):
        if column not in df.columns:
            return {
                "available": False,
                "valid_ratio": 0,
                "reason": "Column missing"
            }

        series = pd.to_numeric(df[column], errors="coerce")
        valid_ratio = series.notna().mean()

        return {
            "available": True,
            "valid_ratio": float(valid_ratio),
            "usable": bool(valid_ratio >= self.min_valid_ratio),
            "reason": "Valid" if valid_ratio >= self.min_valid_ratio else "Too many missing/invalid values"
        }

    def check_timestamp(self, df, timestamp_column):
        if timestamp_column not in df.columns:
            return {
                "available": False,
                "usable": False,
                "reason": "Timestamp missing"
            }

        timestamps = pd.to_datetime(df[timestamp_column], errors="coerce")
        valid_ratio = timestamps.notna().mean()

        return {
            "available": True,
            "usable": valid_ratio >= self.min_valid_ratio,
            "valid_ratio": float(valid_ratio)
        }

    def inspect(self, df):
        report = {}
        for column in df.columns:
            if column.lower() in ["timestamp", "datetime", "date", "time"]:
                continue
            
            report[column] = self.check_column(df, column)
            
        return report
