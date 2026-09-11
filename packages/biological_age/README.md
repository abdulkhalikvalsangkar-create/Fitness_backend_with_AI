# Biological Age Calculator API

Biological age prediction module using the CosinorAge algorithm (ETH Zurich / Arxiv 2509.01089v1) from wearable accelerometer ENMO data. Compatible with Python 3.12.13 and deployable on cPanel via Phusion Passenger.

---

## 1. Expected Input Format

### 1.1 CSV Data File (ENMO Timeseries)

The module requires a CSV file containing **minute-level accelerometer ENMO (Euclidean Norm Minus One)** data.

**File Example** (`sample1.csv`):
```
time,enmo_mg
2023-11-30T19:45:00.000Z,8.669380593092809
2023-11-30T19:46:00.000Z,7.766641409085598
2023-11-30T19:47:00.000Z,7.614175882230335
```

**Column Requirements:**

| Column            | Accepted Names                          | Format / Description                                                                 |
|-------------------|-----------------------------------------|--------------------------------------------------------------------------------------|
| **Timestamp**     | `time`, `timestamp`, `datetime`, `date_time`, `date` | ISO 8601 with Z suffix: `YYYY-MM-DDTHH:MM:SS.sssZ` (e.g. `2023-11-30T19:45:00.000Z`) |
| **ENMO Value**    | `enmo_mg`, `enmo`, `enmomg`, `activity` | Numeric (float) — ENMO in **milligrams (mg)** units, minute-level resolution         |

**Auto-Detection:** Columns are auto-detected by name (case-insensitive). If names are non-standard, the first column is used as timestamp and the second as ENMO.

**Minimum Data Quality:**
- Recommended: **at least 7 consecutive days** of data for reliable results
- Required daily coverage threshold: **50%** (enforced by preprocessor)
- Gaps and partial-day data are handled but reduce accuracy

### 1.2 Request Parameters (API / Function Call)

| Parameter            | Type    | Accepted Values                     | Required | Description                          |
|----------------------|---------|-------------------------------------|----------|--------------------------------------|
| `file`               | CSV     | Uploaded CSV file                   | Yes      | ENMO timeseries data (see 1.1)       |
| `chronological_age`  | float   | 0–120 (years)                       | Yes      | Chronological age of the individual  |
| `gender`             | string  | `male`, `M`, `1`, `female`, `F`, `0`, `w`, `woman` | Yes      | Gender identifier                    |

---

## 2. Generated Output Format

### 2.1 Core Prediction Response (JSON)

```json
{
  "predicted_biological_age": 47.83,
  "chronological_age": 45.0,
  "gender": "male",
  "biological_age_advance": 2.83,
  "cosinor_features": {
    "mesor": 35.6214,
    "amplitude": 28.4571,
    "acrophase": 3.1416
  },
  "input_file": "/path/to/sample1.csv",
  "data_summary": {
    "days_covered": 7.25
  }
}
```

### 2.2 Output Field Descriptions

| Field                                    | Type    | Description                                                                 |
|------------------------------------------|---------|-----------------------------------------------------------------------------|
| `predicted_biological_age`              | float   | Predicted biological age in years (main output)                            |
| `chronological_age`                     | float   | Echo of the input chronological age for reference                          |
| `gender`                                 | string  | Normalized gender: `male` or `female`                                      |
| `biological_age_advance`                | float   | `predicted_biological_age - chronological_age`.<br>**Positive = older than actual age** (health risk indicator)<br>**Negative = younger than actual age** (healthy sign) |
| `cosinor_features.mesor`                | float   | **MESOR** — Midline Estimating Statistic of Rhythm (mean activity level)  |
| `cosinor_features.amplitude`            | float   | **Amplitude** — Peak-to-midline magnitude of the 24h activity rhythm      |
| `cosinor_features.acrophase`            | float   | **Acrophase** — Time of day (in radians, 0–2π) when the rhythm peaks       |
| `data_summary.days_covered`             | float   | Number of days spanned by the input data (validated coverage)              |

### 2.3 Optional Extended Features (when `return_features=True`)

Additionally includes:
```json
{
  "features": {
    "cosinor": { ... },
    "nonparam": { ... },
    "physical_activity": { ... },
    "sleep": { ... }
  }
}
```

---

## 3. cPanel Setup & Deployment (Python 3.12.13)

### 3.1 Prerequisites

- **cPanel account with SSH access** and **Python Selector / Setup Python App** enabled
- **Python 3.12.13** selected in cPanel Python Selector (confirm via `python --version`)
- **Phusion Passenger** enabled (provided by cPanel Python Selector automatically)
- Recommended disk space: **≥ 500 MB** (for numpy/scipy/scikit-learn wheels + compiled packages)
- Recommended memory limit: **≥ 512 MB** (preprocessing and feature extraction are memory-intensive)

### 3.2 Step 1: Create Python Application in cPanel

1. Log in to cPanel → **Setup Python App**
2. Click **Create Application**
3. Configure as follows:
   - **Python version:** `3.12.13`
   - **Application root:** `biological age` (directory name, relative to home)
   - **Application URL:** (choose your domain/subdomain + path)
   - **Application startup file:** `passenger_wsgi.py`
   - **Application Entry point:** `application`
4. Click **CREATE**

After creation, cPanel will show a command like:
```bash
source /home/username/virtualenv/biological_age/3.12/bin/activate && cd /home/username/biological_age
```
Copy this command — you will need it for Step 3.

### 3.3 Step 2: Upload Project Files

Upload the following files to `/home/username/biological_age/`:
```
biological_age/
├── biological_age_calculator.py     (core logic)
├── app.py                           (FastAPI app — REQUIRED, see 3.5)
├── passenger_wsgi.py                (WSGI entry for Passenger)
├── requirements.txt                 (dependencies)
├── sample1.csv                      (optional, for testing)
└── example_usage.py                 (optional, for local testing)
```

### 3.4 Step 3: Install Dependencies via SSH

Connect via SSH and run the **activate command** from Step 2, then install:

```bash
# Activate the virtualenv (copy YOUR command from cPanel)
source /home/username/virtualenv/biological_age/3.12/bin/activate
cd /home/username/biological_age

# Upgrade pip first (critical for Python 3.12 wheel support)
pip install --upgrade pip setuptools wheel

# Install dependencies with binary wheel preference
pip install --only-binary=:all: -r requirements.txt

# If above fails for some packages, fallback to:
pip install -r requirements.txt
```

**Expected installation time:** 3–10 minutes (most packages are wheels; a few may compile)

### 3.5 Step 4: Create the FastAPI App File (`app.py`)

**IMPORTANT:** `passenger_wsgi.py` references `from app import app as asgi_app`. You **must create `app.py`** in the project directory.

Create a file named `app.py` with the following content:

```python
"""
FastAPI Application for Biological Age Calculator
Deployed on cPanel via Phusion Passenger + a2wsgi ASGIMiddleware
"""

import os
import tempfile
from typing import Optional

from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from biological_age_calculator import calculate_biological_age

app = FastAPI(
    title="Biological Age Calculator API",
    description="Predict biological age from wearable ENMO accelerometer data using CosinorAge.",
    version="1.0.0",
)


class HealthResponse(BaseModel):
    status: str
    python_version: str


class CalculateResponse(BaseModel):
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None


@app.get("/", response_model=HealthResponse)
async def root():
    """Health check endpoint."""
    import sys
    return {
        "status": "ok",
        "python_version": sys.version,
    }


@app.get("/health", response_model=HealthResponse)
async def health():
    """Alternate health check."""
    import sys
    return {
        "status": "ok",
        "python_version": sys.version,
    }


@app.post("/api/calculate-biological-age", response_model=CalculateResponse)
async def calculate_age_endpoint(
    file: UploadFile = File(..., description="CSV file with time + enmo_mg columns"),
    chronological_age: float = Form(..., ge=0, le=120, description="Chronological age in years"),
    gender: str = Form(..., description="male/M/1 or female/F/0/woman"),
    return_features: bool = Form(False, description="Include extended wearable features"),
):
    """
    Calculate biological age from an uploaded ENMO CSV file.

    - **file**: CSV with `time` (ISO 8601) and `enmo_mg` (milligram ENMO) columns
    - **chronological_age**: 0–120 years
    - **gender**: male/M/1 or female/F/0
    - **return_features**: include cosinor/nonparam/PA/sleep features in response
    """
    # Validate file type
    if not (file.filename and file.filename.lower().endswith('.csv')):
        raise HTTPException(status_code=400, detail="Only CSV files are accepted.")

    # Read uploaded file and save to temp
    try:
        contents = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read uploaded file: {e}")

    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.csv', delete=False) as f:
            f.write(contents)
            temp_path = f.name

        # Run the calculation
        result = calculate_biological_age(
            file_path=temp_path,
            chronological_age=chronological_age,
            gender=gender,
            return_features=return_features,
        )

        # Clean up absolute paths for public response
        result.pop("input_file", None)

        return {
            "success": True,
            "data": result,
        }

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        # Log on server but don't leak internal details
        print(f"[ERROR] calculate_age_endpoint: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error during calculation. Check server logs.")
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass
```

### 3.6 Step 5: Restart the Passenger App

After uploading files and installing dependencies:

**Option A (via cPanel UI):**
- Go to **Setup Python App** → click the **edit (pencil)** icon next to your app
- Click **RESTART** in the top-right corner

**Option B (via SSH):**
```bash
# Create a "restart" marker file (Passenger monitors this)
touch /home/username/biological_age/tmp/restart.txt
```

### 3.7 Step 6: Verify Deployment

1. **Health check:** Visit `https://your-domain.com/health` in your browser → expect:
   ```json
   {"status":"ok","python_version":"3.12.13"}
   ```

2. **Test the API with curl** (from SSH or locally):
   ```bash
   curl -X POST "https://your-domain.com/api/calculate-biological-age" \
     -F "file=@sample1.csv" \
     -F "chronological_age=45" \
     -F "gender=male" \
     -F "return_features=false"
   ```

---

## 4. Dependencies Compatibility Report (cPanel + Python 3.12.13)

| Package                     | Version     | Python 3.12 | cPanel Wheel | Notes / cPanel-Specific Actions                          |
|-----------------------------|-------------|:-----------:|:------------:|-----------------------------------------------------------|
| `numpy`                     | 1.26.4      | ✅          | ✅ manylinux  | Core dependency; no issues                                 |
| `pandas`                    | 2.2.3       | ✅          | ✅ manylinux  | No issues                                                  |
| `scipy`                     | 1.14.1      | ✅          | ✅ manylinux  | Large wheel (~40MB); may take time to download            |
| `scikit-learn`              | 1.6.1       | ✅          | ✅ manylinux  | No issues                                                  |
| `python-dateutil`           | 2.9.0.post0 | ✅          | ✅ pure Python| No issues                                                  |
| `pytz`                      | 2026.1      | ✅          | ✅ pure Python| No issues                                                  |
| `tzdata`                    | 2026.1      | ✅          | ✅ pure Python| No issues                                                  |
| `scikit-digital-health`     | 0.17.18     | ⚠️          | ⚠️ may compile| Falls back to Cython compilation if no wheel. Ask host to install `gcc` + `python312-devel` if it fails. |
| `statsmodels`               | 0.14.4      | ✅          | ✅ manylinux  | No issues                                                  |
| `CosinorPy`                 | 3.1         | ✅          | ✅ pure Python| No issues                                                  |
| `seaborn`                   | 0.13.2      | ✅          | ✅ pure Python| Not required at runtime for API (used only if plotting)   |
| `matplotlib`                | 3.9.4       | ✅          | ✅ manylinux  | May need `libpng-devel`, `freetype-devel`, `libjpeg-devel` on host |
| `claid`                     | 0.6.4       | ✅          | ⚠️ needs grpcio | Indirect dependency; lightweight                           |
| `protobuf`                  | 4.25.3      | ✅          | ✅ manylinux  | No issues                                                  |
| `grpcio`                    | 1.59.3      | ✅          | ✅ manylinux  | Large binary wheel; allow 2–3 minutes for download        |
| `cosinorage`                | 1.0.8       | ✅          | ✅ pure Python| Main CosinorAge library                                    |
| `fastapi`                   | 0.115.12    | ✅          | ✅ pure Python| Web framework; no issues                                   |
| `uvicorn`                   | 0.34.2      | ✅          | ✅ manylinux  | ASGI server; not used directly on cPanel (Passenger runs WSGI via a2wsgi) |
| `a2wsgi`                    | 1.10.8      | ✅          | ✅ pure Python| Bridges FastAPI (ASGI) → Passenger (WSGI); **required**   |
| `python-multipart`          | 0.0.20      | ✅          | ✅ pure Python| Required for FastAPI `File()` uploads                     |

### ✅ Overall Compatibility: ACCEPTABLE for cPanel
All required packages have Python 3.12 support. The majority ship as precompiled manylinux wheels compatible with CloudLinux/CentOS/RHEL-based cPanel servers.

---

## 5. Important Instructions & Troubleshooting

### 5.1 cPanel-Specific Configuration

**Increase Memory Limit** (if calculations fail silently):
1. cPanel → **MultiPHP INI Editor** → Select domain → Edit:
   ```ini
   memory_limit = 512M
   max_execution_time = 300
   upload_max_filesize = 64M
   post_max_size = 64M
   ```
2. Also set in `.htaccess` in the app directory:
   ```apache
   <IfModule mod_passenger.c>
     PassengerAppEnv production
     PassengerMemoryLimit 512
   </IfModule>
   ```

**Enable Passenger Error Logging** (for debugging 500 errors):
Check these files via SSH or cPanel File Manager:
- App-level errors: `/home/username/virtualenv/biological_age/3.12/logs/passenger.log`
- Apache errors: `/home/username/logs/` or `/usr/local/apache/logs/error_log` (ask host)

### 5.2 Common Issues & Fixes

| Issue                                            | Probable Cause                                                  | Fix                                                                 |
|--------------------------------------------------|-----------------------------------------------------------------|---------------------------------------------------------------------|
| `ModuleNotFoundError: No module named 'app'`     | `app.py` is missing                                             | Create `app.py` as shown in Section 3.5                              |
| `ModuleNotFoundError: No module named 'numpy'`   | Dependencies not installed in the correct virtualenv            | Re-run the `source .../activate` command (from cPanel Setup Python App) then `pip install -r requirements.txt` |
| 502 Bad Gateway / Passenger fails to start       | Syntax error in `app.py` or `passenger_wsgi.py`, or missing deps | Check passenger.log; test locally: `python -c "from app import app; print('OK')"` |
| 500 Internal Server Error on `/api/...`          | Data quality issue or unhandled exception                       | Check server error log for traceback; ensure CSV has valid columns + ≥50% daily coverage |
| pip install fails compiling `scikit-digital-health` | Missing C compiler / Python dev headers                     | Ask cPanel host: "Please install gcc, python312-devel, and ensure pip can build C extensions for my account" |
| Calculation returns `RuntimeError: CosinorAge returned no predictions` | Data has insufficient coverage or invalid format | Verify CSV has >=50% daily coverage; confirm columns are correctly detected; try with `sample1.csv` first |
| `Pip subprocess error` while installing wheels   | Disk quota exceeded or /tmp is full                             | Check disk quota in cPanel; ask host for temporary `/tmp` space increase |
| Uploads fail at >2MB                             | `upload_max_filesize` / `post_max_size` too small              | Increase both via MultiPHP INI Editor to at least 64M                |

### 5.3 Pre-Deployment Checklist (Before Going Live)

1. ✅ Python 3.12.13 selected in cPanel Setup Python App
2. ✅ `passenger_wsgi.py` startup file is set correctly (default name)
3. ✅ `app.py` exists and `from app import app` works (test via SSH + activate)
4. ✅ All packages installed: `pip list` shows every package from `requirements.txt`
5. ✅ Health endpoint returns JSON: `/health` responds `{"status":"ok"...}`
6. ✅ Test POST with `sample1.csv` + age=45 + gender=male returns valid prediction
7. ✅ Memory limit ≥ 512MB; max_execution_time ≥ 120s
8. ✅ Upload size limits ≥ 64MB (for longer multi-week CSVs)
9. ✅ `.htaccess` exists and does not disable Passenger

### 5.4 Security Best Practices

- **HTTPS only:** Do NOT expose the API over plain HTTP. cPanel AutoSSL provides free certificates.
- **Rate limiting:** Add protection via cPanel ModSecurity or Cloudflare to prevent abuse.
- **File validation:** The app validates `.csv` extension; for extra safety, whitelist `text/csv` MIME type.
- **No secrets in code:** `app.py` contains no secrets; keep it that way.
- **Keep dependencies patched:** Every 3 months, reactivate the venv and run:
  ```bash
  pip list --outdated
  pip install --upgrade numpy pandas scipy scikit-learn fastapi uvicorn a2wsgi
  touch tmp/restart.txt
  ```

---

## 6. Local Development (Without cPanel)

For testing on your local machine (Windows / macOS / Linux):

```bash
# 1. Create virtualenv
python -m venv venv

# 2. Activate
# Windows (Powershell):
.\venv\Scripts\Activate.ps1
# macOS/Linux:
source venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 4. Test core module directly
python example_usage.py

# 5. Run FastAPI dev server (uvicorn)
uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# 6. Open http://localhost:8000/docs  (Swagger UI for manual testing)
```

---

## 7. API Endpoints Summary

| Method | Path                                  | Purpose                                              | Auth |
|--------|---------------------------------------|------------------------------------------------------|------|
| GET    | `/` or `/health`                      | Health check + Python version info                   | None |
| GET    | `/docs`                               | Swagger UI interactive documentation                | None |
| GET    | `/openapi.json`                       | OpenAPI 3.0 spec (machine-readable)                 | None |
| POST   | `/api/calculate-biological-age`       | Upload CSV + params → get biological age prediction | None |

---

## 8. Reference

**CosinorAge Algorithm:** Based on:
- ETH Zurich CosinorAge-Calculator repository
- Arxiv preprint 2509.01089v1: "CosinorAge: Biological Age Estimation from Wearable Circadian Rhythm Features"

---
*Document last updated for deployment on cPanel (Python 3.12.13 / Phusion Passenger).*
