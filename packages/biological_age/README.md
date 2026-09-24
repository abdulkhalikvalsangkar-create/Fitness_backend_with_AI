# Biological age calculator

Estimates biological age from ordinary daily wearable summaries — activity/
steps, sleep hours, resting heart rate, HRV — rather than a raw accelerometer
timeseries. Ported from an intern-built engine
(`AdaptiveBiologicalAgeEngine`); ties into the API via the
`biological_age_calculator` action (`apps/api/actions.py`).

Replaces an earlier implementation built on the third-party `cosinorage`
package, which required a minute-level ENMO accelerometer CSV — a much
harder thing for a mobile app to produce than the daily numbers most
wearable integrations already expose.

## Model provenance — read before trusting these numbers

`models/model_a.joblib` and `models/model_b.joblib` are two
`RandomForestRegressor`s (scikit-learn), confirmed directly with the intern
who built them:

- Trained on the first 25,000 rows of a Kaggle file,
  `whoop_fitness_dataset_100k.csv` (100k rows total) — the file's own
  provenance/cohort composition hasn't been independently verified.
- **No train/test split.** The training script fits and evaluates
  (`r2_score`) on the same 25k rows. The R² values embedded in the joblib
  bundles (`bundle["metrics"]`, ~0.99) are training-set fit metrics, not a
  measure of real-world accuracy.
- **The "biological age" training label was not measured — it was computed
  by a formula from the same inputs**, e.g.
  `biological_age = chronological_age + f(hrv, resting_hr, sleep, recovery, activity_amplitude)`,
  clipped to ±12 years of adjustment. The models were trained to reproduce
  that formula, which is exactly why they fit it almost perfectly.
- No independent validation (against real wearable exports, a clinical
  cohort, or an external test set) has been done.

**Treat this as a plausible, self-consistent heuristic, not a validated
biological-age predictor**, until it's retrained on a documented dataset
with an independently-defined label and evaluated on a held-out,
subject-level split. `prediction_method`/`model_type` in every response
name exactly which tier produced a given number, so this stays traceable.

## Architecture

```
input (file or features dict)
  -> schema_detector.py    vendor column-name normalisation (Apple/Garmin/WHOOP/Fitbit/...)
  -> validators.py         per-column data-quality check (>=70% valid ratio)
  -> engine.py              AdaptiveBiologicalAgeEngine — orchestrates the above,
                             then hands validated features to:
  -> age_model.py           ModelRegistry — cascading model selection:
       Priority 1  Model A   RandomForest: activity + sleep + resting HR + HRV
       Priority 2  Model B   RandomForest: activity + sleep + resting HR (no HRV)
       Priority 3  Fallback  clinical rule engine: activity OR sleep, no ML
       Priority 4  none      insufficient data — no prediction is fabricated
  -> missing_data_engine.py  what was used / unavailable / data_quality_percent
```

`feature_registry.py` and `parameter_validator.py` are the declarative
feature catalog and the manual-input clinical range checks
(`PARAM_RULES` — resting HR 25–130 bpm, HRV 1–300 ms, sleep 0–24h, etc).

Gender is accepted end-to-end but not consumed by any model's math today —
it's echoed back, not required.

## Two entry points (`biological_age_calculator.py`)

```python
calculate_biological_age(contents: bytes, filename: str, chronological_age, gender=None, return_features=False)
```
Parses a CSV or JSON file of daily metrics. Raw tri-axial accelerometer
(`x_g`/`y_g`/`z_g`) is reduced to ENMO automatically if no ENMO column is
already present.

```python
calculate_biological_age_from_features(chronological_age, features: dict, gender=None)
```
No file — validates a `features` dict directly
(e.g. `{"activity_mean": 8000, "sleep_hours": 7.5, "resting_heart_rate": 60, "hrv": 55}`)
and runs the same cascade. Both raise `ValueError` for bad input; the action
handler maps that to a 400, and a `can_predict: false` result (ran, but too
little data for any tier) to a 422.

## Model file size

The two joblib bundles are stored **joblib-compressed** (`compress=3`) —
~14 MB and ~13 MB rather than their original ~45 MB/~41 MB, verified to
predict identically before and after compression. This matters on a cPanel
shared host: smaller git deploys, less disk quota used, less memory
duplicated across Passenger worker processes.

The bundles were pickled under a newer scikit-learn than the one pinned
here (`scikit-learn==1.6.1`); verified to load and predict correctly anyway
(`InconsistentVersionWarning` is expected and is suppressed in
`biological_age_calculator.py`, not an error).

## Not carried over

`model_c.joblib` and `model_cosinor.joblib` (present in the source repo but
not used by any active code path) were dropped — dead weight from an
earlier iteration. No cosinor-rhythm code or dependency (the previous
`cosinorage`/`scikit-digital-health`/`CosinorPy` chain) is part of this
module.
