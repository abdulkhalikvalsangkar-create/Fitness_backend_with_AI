import os
import logging
from typing import Dict, Any, Optional, Union
from pathlib import Path

from cosinorage.datahandlers.genericdatahandler import GenericDataHandler
from cosinorage.features.features import WearableFeatures
from cosinorage.bioages.cosinorage import CosinorAge
import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DEFAULT_PREPROCESS_ARGS = {
    'required_daily_coverage': 0.5,
    'autocalib_sd_criter': 0.00013,
    'autocalib_sphere_crit': 0.02,
    'filter_type': 'lowpass',
    'filter_cutoff': 2,
    'wear_sd_crit': 0.00013,
    'wear_range_crit': 0.00067,
    'wear_window_length': 45,
    'wear_window_skip': 7,
}

DEFAULT_FEATURES_ARGS = {
    'sleep_ck_sf': 0.0025,
    'sleep_rescore': True,
    'pa_cutpoint_sl': 15,
    'pa_cutpoint_lm': 35,
    'pa_cutpoint_mv': 70,
}


def _validate_csv_structure(file_path: str) -> Dict[str, Any]:
    """
    Validate CSV file structure and auto-detect columns.
    
    Returns a dict with detected column info.
    """
    df = pd.read_csv(file_path, nrows=5)
    columns = df.columns.tolist()
    columns_lower = [c.lower().strip() for c in columns]

    time_column = None
    enmo_column = None

    for i, col in enumerate(columns_lower):
        if col in ['time', 'timestamp', 'datetime', 'date_time', 'date']:
            time_column = columns[i]
        if col in ['enmo_mg', 'enmo', 'enmomg', 'activity']:
            enmo_column = columns[i]

    if time_column is None and len(columns) >= 1:
        time_column = columns[0]
    if enmo_column is None and len(columns) >= 2:
        enmo_column = columns[1]

    sample_time_val = str(df[time_column].iloc[0]) if time_column else ""
    time_format = "datetime"
    if "T" in sample_time_val or "Z" in sample_time_val:
        time_format = "datetime"
    else:
        time_format = "datetime"

    result = {
        'columns': columns,
        'time_column': time_column,
        'enmo_column': enmo_column,
        'time_format': time_format,
    }

    logger.info(f"Detected CSV structure: time={time_column}, enmo={enmo_column}, format={time_format}")
    return result


def load_enmo_csv(
    file_path: Union[str, Path],
    time_column: Optional[str] = None,
    enmo_column: Optional[str] = None,
    time_format: Optional[str] = None,
    time_zone: Optional[str] = None,
    preprocess_args: Optional[Dict[str, Any]] = None,
) -> GenericDataHandler:
    """
    Load ENMO (Euclidean Norm Minus One) data from a CSV file.
    
    Args:
        file_path: Path to the CSV file.
        time_column: Name of the timestamp column. Auto-detected if None.
        enmo_column: Name of the ENMO value column. Auto-detected if None.
        time_format: Format of timestamps ('iso', 'datetime', 'unix'). Auto-detected if None.
        time_zone: Timezone string (e.g., 'UTC'). Set to None for ISO format to avoid tz conflicts.
        preprocess_args: Override default preprocessing arguments.
    
    Returns:
        GenericDataHandler instance with loaded and processed data.
    """
    file_path = str(file_path)

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"CSV file not found: {file_path}")

    detected = _validate_csv_structure(file_path)

    if time_column is None:
        time_column = detected['time_column']
    if enmo_column is None:
        enmo_column = detected['enmo_column']
    if time_format is None:
        time_format = detected['time_format']

    if time_format in ['iso', 'datetime']:
        time_zone = None

    if preprocess_args is None:
        preprocess_args = {}
    merged_preprocess = {**DEFAULT_PREPROCESS_ARGS, **preprocess_args}

    data_columns = [enmo_column] if enmo_column else []

    logger.info(f"Loading CSV: {file_path}")
    logger.info(f"  Time column: {time_column}")
    logger.info(f"  ENMO column: {enmo_column}")
    logger.info(f"  Time format: {time_format}")
    logger.info(f"  Time zone: {time_zone}")

    handler = GenericDataHandler(
        file_path=file_path,
        data_format="csv",
        data_type="enmo-mg",
        time_format=time_format,
        time_column=time_column,
        time_zone=time_zone,
        data_columns=data_columns,
        preprocess_args=merged_preprocess,
        verbose=False,
    )

    logger.info("CSV data loaded successfully.")
    return handler


def extract_features(
    handler: GenericDataHandler,
    features_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Extract comprehensive wearable features from loaded data.
    
    Args:
        handler: DataHandler instance with loaded data.
        features_args: Override default feature extraction arguments.
    
    Returns:
        Dict containing cosinor, nonparametric, physical_activity, sleep features,
        and the ML-ready dataframe.
    """
    if features_args is None:
        features_args = {}
    merged_features = {**DEFAULT_FEATURES_ARGS, **features_args}

    logger.info("Extracting wearable features...")
    wf = WearableFeatures(handler, features_args=merged_features)
    features = wf.get_features()
    ml_data = wf.get_ml_data()

    result = {
        'cosinor': features.get('cosinor', {}),
        'nonparam': features.get('nonparam', {}),
        'physical_activity': features.get('physical_activity', {}),
        'sleep': features.get('sleep', {}),
        'ml_dataframe': ml_data,
    }

    logger.info("Feature extraction complete.")
    logger.info(f"  Cosinor features: {list(result['cosinor'].keys()) if result['cosinor'] else 'None'}")
    logger.info(f"  Nonparam features: {list(result['nonparam'].keys()) if result['nonparam'] else 'None'}")
    logger.info(f"  PA features: {list(result['physical_activity'].keys()) if result['physical_activity'] else 'None'}")
    logger.info(f"  Sleep features: {list(result['sleep'].keys()) if result['sleep'] else 'None'}")

    return result


def predict_biological_age(
    handler: GenericDataHandler,
    chronological_age: float,
    gender: str,
) -> Dict[str, Any]:
    """
    Predict biological age using the CosinorAge model.
    
    Args:
        handler: DataHandler instance with loaded and processed data.
        chronological_age: Chronological age of the individual in years.
        gender: Gender identifier ('M', 'male', 'F', 'female', etc.).
    
    Returns:
        Dict containing predicted biological age and related metrics.
    """
    gender_lower = str(gender).strip().lower()
    if gender_lower in ['m', 'male', '1']:
        gender_code = 'male'
    elif gender_lower in ['f', 'female', '0', 'w', 'woman']:
        gender_code = 'female'
    else:
        raise ValueError(f"Unsupported gender: {gender}. Use 'male'/'M' or 'female'/'F'.")

    if not isinstance(chronological_age, (int, float)) or chronological_age <= 0:
        raise ValueError(f"Invalid chronological age: {chronological_age}. Must be a positive number.")

    logger.info(f"Predicting biological age (chronological_age={chronological_age}, gender={gender_code})...")

    record = [{
        'handler': handler,
        'age': float(chronological_age),
        'gender': gender_code,
    }]

    cosinor_age = CosinorAge(record)
    predictions = cosinor_age.get_predictions()

    if not predictions or len(predictions) == 0:
        raise RuntimeError("CosinorAge returned no predictions. Check data quality.")

    prediction = predictions[0]

    if 'cosinorage' not in prediction:
        raise RuntimeError(f"CosinorAge prediction missing 'cosinorage' field. Got: {prediction}")

    predicted_age = prediction['cosinorage']
    advance = predicted_age - float(chronological_age)

    result = {
        'predicted_biological_age': float(predicted_age),
        'chronological_age': float(chronological_age),
        'gender': gender_code,
        'biological_age_advance': float(advance),
        'cosinor_features': {
            'mesor': prediction.get('mesor'),
            'amplitude': prediction.get('amp1'),
            'acrophase': prediction.get('phi1'),
        },
        'raw_prediction': {k: (None if (isinstance(v, float) and (pd.isna(v) or np.isinf(v))) else v)
                           for k, v in prediction.items()},
    }

    logger.info(f"Biological age prediction complete:")
    logger.info(f"  Chronological age: {result['chronological_age']:.2f} years")
    logger.info(f"  Predicted biological age: {result['predicted_biological_age']:.2f} years")
    logger.info(f"  Biological age advance: {result['biological_age_advance']:.2f} years")

    return result


def calculate_biological_age(
    file_path: Union[str, Path],
    chronological_age: float,
    gender: str,
    time_column: Optional[str] = None,
    enmo_column: Optional[str] = None,
    time_format: Optional[str] = None,
    time_zone: Optional[str] = None,
    preprocess_args: Optional[Dict[str, Any]] = None,
    features_args: Optional[Dict[str, Any]] = None,
    return_features: bool = False,
) -> Dict[str, Any]:
    """
    End-to-end biological age calculation from a CSV file.
    
    This is the main convenience function that:
        1. Loads ENMO data from CSV
        2. Extracts wearable features
        3. Predicts biological age using CosinorAge
    
    Args:
        file_path: Path to the CSV file with ENMO timeseries data.
        chronological_age: Chronological age of the individual in years.
        gender: Gender ('male'/'M' or 'female'/'F').
        time_column: Optional timestamp column name (auto-detected if None).
        enmo_column: Optional ENMO value column name (auto-detected if None).
        time_format: Optional timestamp format (auto-detected if None).
        time_zone: Optional timezone (None for ISO format).
        preprocess_args: Optional preprocessing overrides.
        features_args: Optional feature extraction overrides.
        return_features: If True, include extracted features in output.
    
    Returns:
        Dict with biological age prediction and optionally features/metadata.
    """
    file_path = str(file_path)

    handler = load_enmo_csv(
        file_path=file_path,
        time_column=time_column,
        enmo_column=enmo_column,
        time_format=time_format,
        time_zone=time_zone,
        preprocess_args=preprocess_args,
    )

    features = extract_features(handler, features_args=features_args)

    age_result = predict_biological_age(
        handler=handler,
        chronological_age=chronological_age,
        gender=gender,
    )

    result = {
        **age_result,
        'input_file': os.path.abspath(file_path),
        'data_summary': {
            'days_covered': _estimate_days_covered(features['ml_dataframe']),
        },
    }

    if return_features:
        result['features'] = {k: v for k, v in features.items() if k != 'ml_dataframe'}

    return result


def _estimate_days_covered(df: pd.DataFrame) -> Optional[float]:
    """Estimate number of days covered by the data."""
    if df is None or df.empty:
        return None
    try:
        idx = df.index
        if hasattr(idx, 'min') and hasattr(idx, 'max'):
            t_min = idx.min()
            t_max = idx.max()
            delta = pd.Timestamp(t_max) - pd.Timestamp(t_min)
            return round(delta.total_seconds() / 86400.0, 2)
    except Exception as e:
        logger.debug(f"Could not estimate days covered: {e}")
    return None
