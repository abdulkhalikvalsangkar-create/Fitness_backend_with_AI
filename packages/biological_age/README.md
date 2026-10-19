# Biological age calculator

Estimates biological age from daily wearable/fitness parameters, using a
single "missing-aware" model instead of a multi-tier cascade. Ported from
an intern-built project (`missing_aware_model.py` /
`predict_from_terminal.py`); ties into the API via the
`biological_age_calculator` action (`apps/api/actions.py`), which needed no
changes for this swap — the module's public functions kept the exact same
signatures and response shape as before.

This replaces the previous two-tier RandomForest cascade (Model A / Model B
/ a rule-based fallback), which itself had replaced an earlier CosinorAge
implementation requiring a raw ENMO accelerometer timeseries.

## Model provenance — read before trusting these numbers

**The same caution from the previous module applies here, confirmed
directly with the author:**

- Trained on a public Kaggle file, `Whoop-apple-fitbit-bioage.csv`
  (106,264 rows, genuinely split 70/15/15 train/validation/test — this
  part is more rigorous than the previous module, which had no held-out
  split at all).
- **The `estimated_biological_age` target was not measured — it was
  computed by a formula** from the same inputs (HRV, resting heart rate,
  sleep, recovery, a "cosinor activity amplitude" proxy), clipped to
  ±12 years of adjustment from chronological age. The model was trained to
  reproduce that formula; the reported R²≈0.986 measures how well it does
  that, not how well the formula describes real biological aging.
- The source dataset has a confirmed, uncleaned data-quality defect: **2.5%
  of rows have `calories_burned` > 100,000** (physically impossible for a
  day; max value is 975,000) and **421 rows have `resting_heart_rate` <
  25 bpm** (109 of those under 10). Nothing in the training script filters
  these out before fitting.
- `estimated_biological_age` correlates 0.9947 with chronological `age`
  alone — age is also one of the model's 12 input features, so most of the
  reported accuracy comes from the near-identity age→age mapping, not from
  the wearable data doing real work.

**Treat this as a plausible, self-consistent heuristic, not a validated
biological-age predictor** — same as the module it replaced. The practical
upside of this version is architectural, not scientific: a single model
that degrades gracefully as inputs go missing, a genuine held-out
evaluation, and a much lighter deployment footprint (see below).

## Architecture

```
features dict (or one row of a CSV/JSON file)
  -> parameter_validator.py   range/type check against the 12 recognised
                               features; unrecognised keys are a warning,
                               not an error
  -> biological_age_calculator.py
       - "age" always comes from chronological_age, always available
       - "gender" comes from the top-level param if not already in features
       - builds a 1/0 availability mask for all 12 features
  -> missing_aware_biological_age_model.joblib
       preprocessor: ColumnTransformer
         numeric    -> SimpleImputer(median) -> StandardScaler
         categorical -> SimpleImputer(constant) -> OneHotEncoder(ignore unknown)
       model: LinearRegression over [encoded features, 12 mask flags] (46 inputs)
  -> biological_age, clipped to [1, 120]
```

The 12 recognised features: `age`, `gender`, `height_cm`, `weight_kg`,
`hrv`, `resting_heart_rate`, `calories_burned`, `activity_type`,
`fitness_level`, `recovery_score`, `day_strain`, `sleep_hours`.

Unlike the previous module's discrete tiers (Model A needs 4 exact
features, Model B needs 3, else a rule-based fallback), this model accepts
*any* subset of the 12 — the mask tells it which values are real and which
were imputed, so there's no hard cutover between "enough data" and "not
enough," just a continuous accuracy gradient (measured by the source
project across every missing-count from 0 to 11 — see its Progress Report
for the exact MAE/RMSE/R² at each count).

## Behaviour differences from the previous module — read if integrating

- **`gender` is now an actual model input**, not just an echoed-back field.
  The app already sends it on every call, so no app change is needed, but
  it now measurably affects the prediction (one-hot encoded).
- **The recognised `features` keys changed.** The previous module
  recognised `activity_mean`/`steps`/`vo2_max`/`systolic_bp`/etc. (see its
  `PARAM_RULES`). This model has no equivalent for most of those — it wants
  `calories_burned`/`activity_type`/`fitness_level`/`day_strain` instead. A
  few obvious synonyms are aliased (`rhr`→`resting_heart_rate`,
  `sleep`→`sleep_hours`, `weight`→`weight_kg`, `height`→`height_cm`) but
  there's no equivalent for `activity_mean`/`steps`/`vo2_max` — those are
  now simply unrecognised (ignored with a warning, not an error).
- **No "insufficient data" floor in the model itself** — left bare, it will
  happily impute every missing value and return something close to the
  training-set mean age even for a completely empty request. This module
  adds that floor back explicitly: at least one real feature besides `age`
  is required, or the response comes back as `can_predict: false` (→ a 422
  from the action handler), matching the previous module's behaviour for
  "ran, but not enough data."
- **Response field names are unchanged** (`predicted_biological_age`,
  `biological_age_advance`, `features_used`, `data_quality_percent`,
  `unavailable`, `explanations`, `can_predict`, etc.) — `apps/api/actions.py`
  needed no changes for this swap.

## Deployment footprint

The joblib artifact is **5.7 KB** (a `LinearRegression` with 46
coefficients), versus ~27 MB for the two compressed RandomForest bundles it
replaces. No new dependency: `pandas`/`scikit-learn`/`joblib` were already
pinned in the root `requirements.txt` for the previous module, and this one
uses nothing beyond plain scikit-learn (`LinearRegression`,
`ColumnTransformer`, `Pipeline`, `SimpleImputer`, `StandardScaler`,
`OneHotEncoder`) — the package list is unchanged.

## Not carried over

The source project's standalone pieces — `baseline_model.py` (a
no-missing-data comparison baseline), `predict_from_terminal.py` (its
terminal demo app), `prepare_missing_aware_data.py` and the prepared
train/validation/test CSVs, and `missing_aware_model.py` itself (the
training script) — are not part of this module. They're useful for
retraining or re-evaluating the model, not for serving predictions; keep
them with the training project, not here.
