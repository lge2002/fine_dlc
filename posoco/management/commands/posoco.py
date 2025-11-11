from django.core.management.base import BaseCommand
import os
import requests
import tabula
import json
import re
import tempfile
from datetime import datetime, timedelta
from posoco.models import PosocoTableA, PosocoTableG
import pandas as pd
# new import for reading PDF content
try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None

# --- Constants ---
API_URL = "https://webapi.grid-india.in/api/v1/file"
BASE_URL = "https://webcdn.grid-india.in/"
SAVE_DIR = "downloads/POSOCO"

# --- payload: initial (today) - but we will fall back if API returns nothing ---
payload = {
    "_source": "GRDW",
    "_type": "DAILY_PSP_REPORT",
    "_fileDate": datetime.now().strftime("%Y-%m-%d"),
    "_month": datetime.now().strftime("%m")
}

# --- Helper Functions ---
def make_report_dir(base_dir, desired_date=None):
    """Create a timestamped subfolder inside POSOCO/. 
       If desired_date is provided, include that date in the folder name so past-date runs are kept distinct.
    """
    # desired_date may be a datetime.date or datetime or string parsed outside
    if desired_date:
        try:
            if isinstance(desired_date, datetime):
                date_part = desired_date.strftime("%Y-%m-%d")
            else:
                # assume date-like (datetime.date)
                date_part = desired_date.strftime("%Y-%m-%d")
        except Exception:
            date_part = datetime.now().strftime("%Y-%m-%d")
    else:
        date_part = datetime.now().strftime("%Y-%m-%d")

    timestamp = datetime.now().strftime("%H-%M-%S")
    # folder includes the requested date and a time suffix to avoid collisions
    report_dir = os.path.join(base_dir, f"report_{date_part}_{timestamp}")
    os.makedirs(report_dir, exist_ok=True)
    return report_dir, timestamp

def _parse_date_from_string(s):
    """Try several date patterns and return a datetime or None."""
    if not s:
        return None
    s = str(s)
    patterns = [
        (r'(\d{4}-\d{2}-\d{2})', '%Y-%m-%d'),
        (r'(\d{2}-\d{2}-\d{4})', '%d-%m-%Y'),
        (r'(\d{2}\d{2}\d{4})', '%d%m%Y'),
        (r'(\d{8})', '%Y%m%d'),
    ]
    for pat, fmt in patterns:
        m = re.search(pat, s)
        if m:
            try:
                return datetime.strptime(m.group(1), fmt)
            except Exception:
                continue
    try:
        return datetime.fromisoformat(s.split(".")[0])
    except Exception:
        return None

def _post_and_get_retdata(api_url, payload, timeout=30):
    """POST and return parsed JSON (or None on failure)."""
    try:
        resp = requests.post(api_url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        print(f"❌ Network/API error when posting payload {payload}: {e}")
        return None
    except ValueError as e:
        print(f"❌ Invalid JSON response for payload {payload}: {e}")
        return None

def _download_to_temp(url, timeout=60):
    """Download URL to a temporary file and return its path (or None)."""
    try:
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        with open(tmp.name, "wb") as fh:
            for chunk in r.iter_content(1024):
                fh.write(chunk)
        return tmp.name
    except Exception as e:
        print(f"❌ Error downloading temp PDF {url}: {e}")
        return None

def _extract_report_date_from_pdf(pdf_path):
    """
    Read first page text of pdf_path and try to find a date of the form:
    DD.MM.YYYY or DD-MM-YYYY or YYYY-MM-DD or DD/MM/YYYY
    Returns a datetime.date or None.
    """
    if PdfReader is None:
        # PyPDF2 not available
        return None
    try:
        reader = PdfReader(pdf_path)
        if len(reader.pages) == 0:
            return None
        first_page = reader.pages[0]
        text = ""
        try:
            text = first_page.extract_text() or ""
        except Exception:
            # fallback if extract_text fails
            text = ""
        if not text:
            return None
        # search common date patterns
        patterns = [
            r'(\b\d{2}[.\-/]\d{2}[.\-/]\d{4}\b)',  # DD.MM.YYYY or DD-MM-YYYY or DD/MM/YYYY
            r'(\b\d{4}[.\-/]\d{2}[.\-/]\d{2}\b)',  # YYYY-MM-DD
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                ds = m.group(1)
                for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
                    try:
                        dt = datetime.strptime(ds, fmt)
                        return dt.date()
                    except Exception:
                        continue
        return None
    except Exception as e:
        print(f"❌ Error reading PDF for printed date: {e}")
        return None

def fetch_latest_pdf(api_url, base_url, payload, report_dir, timestamp, desired_date=None):
    """Fetches and downloads the latest PDF report, preferring a file that matches today's printed date.
       If desired_date (datetime.date) is provided, the final saved PDF will be named with that date.
       NOTE: Selection/download logic is unchanged; only final filename uses desired_date if provided.
    """
    try:
        print("🔎 Initial API payload:", payload)
        response_data = _post_and_get_retdata(api_url, payload)

        # fallback attempts (no-dates, last 7 days)
        if not response_data or not response_data.get("retData"):
            payload_no_dates = {k: v for k, v in payload.items() if k not in ("_fileDate", "_month")}
            print("⚠️ No files for initial payload — trying without date filters:", payload_no_dates)
            response_data = _post_and_get_retdata(api_url, payload_no_dates)

        if not response_data or not response_data.get("retData"):
            print("⚠️ Still no files. Trying last 7 days individually...")
            for days_back in range(0, 7):
                d = datetime.now() - timedelta(days=days_back)
                daily_payload = {
                    "_source": payload.get("_source"),
                    "_type": payload.get("_type"),
                    "_fileDate": d.strftime("%Y-%m-%d"),
                    "_month": d.strftime("%m")
                }
                print(f"   → trying payload for {d.strftime('%Y-%m-%d')}")
                resp = _post_and_get_retdata(api_url, daily_payload)
                if resp and resp.get("retData"):
                    response_data = resp
                    print(f"✅ Found retData for date {d.strftime('%Y-%m-%d')}")
                    break

        if not response_data or not response_data.get("retData"):
            print("⚠️ No files found after all fallback attempts.")
            return None

        data = response_data
        pdf_files = [f for f in data["retData"] if f.get("MimeType") == "application/pdf"]
        if not pdf_files:
            print("⚠️ No PDF files available in retData")
            return None

        # helpers for date inference
        def infer_date_safe(item):
            try:
                for fld in ("FileDate", "CreatedOn", "LastModified", "fileDate", "createdOn", "lastModified"):
                    val = item.get(fld)
                    if val:
                        vstr = str(val)
                        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                            try:
                                return datetime.strptime(vstr.split(".")[0], fmt)
                            except Exception:
                                continue
                        parsed = _parse_date_from_string(vstr)
                        if parsed:
                            return parsed
                for fld in ("FilePath", "FileName", "Path", "fileName", "filepath", "Filepath"):
                    parsed = _parse_date_from_string(str(item.get(fld, "")))
                    if parsed:
                        return parsed
            except Exception:
                pass
            return None

        # common date string formats for "today" to search in filenames
        local_today = datetime.now().date()
        utc_today = datetime.utcnow().date()
        patterns_for_today = [
            local_today.strftime("%d%m%Y"),   # DDMMYYYY
            local_today.strftime("%Y%m%d"),   # YYYYMMDD
            local_today.strftime("%Y-%m-%d"), # YYYY-MM-DD
            utc_today.strftime("%d%m%Y"),
            utc_today.strftime("%Y%m%d"),
            utc_today.strftime("%Y-%m-%d"),
        ]

        def filename_contains_today(item):
            s = " ".join(str(item.get(k, "") or "") for k in ("FileName", "FilePath", "Path"))
            normal = re.sub(r'[_\-/\s]+', '', s).lower()
            for p in patterns_for_today:
                if p.replace("-", "") in normal:
                    return True
            return False

        # 1) pick by filename match for today (fast)
        candidates = []
        for item in pdf_files:
            if filename_contains_today(item):
                print("Found filename/path match for today:", item.get("FileName") or item.get("FilePath"))
                candidates.append(item)

        # 2) If none found by filename, attempt explicit date field match
        if not candidates:
            for item in pdf_files:
                parsed = infer_date_safe(item)
                if parsed:
                    if parsed.date() == local_today or parsed.date() == utc_today:
                        print("Found explicit metadata date match for today:", item.get("FileName") or item.get("FilePath"), parsed)
                        candidates.append(item)

        # 3) If still no candidates, fallback to most recent ordering (but we'll inspect them too)
        if not candidates:
            def file_date_key(item):
                parsed = infer_date_safe(item)
                if parsed:
                    return parsed
                return datetime(1970, 1, 1)
            pdf_files_sorted = sorted(pdf_files, key=file_date_key, reverse=True)
            for i, f in enumerate(pdf_files_sorted[:8]):
                try:
                    inf_date = file_date_key(f)
                except Exception:
                    inf_date = None
                print(f"candidate [{i}]: FileName={f.get('FileName')} FilePath={f.get('FilePath')} inferred_date={inf_date}")
            candidates = pdf_files_sorted[:8]  # inspect top candidates

        # Inspect candidate PDFs by reading their printed date on the first page.
        selected = None
        temp_files_to_cleanup = []
        for item in candidates:
            file_path_rel = item.get("FilePath") or item.get("Path") or item.get("Filepath")
            if not file_path_rel:
                continue
            download_url = base_url.rstrip("/") + "/" + file_path_rel.lstrip("/")
            if "?" not in download_url:
                download_url = download_url + f"?cachebust={int(datetime.now().timestamp())}"
            # download to temp and inspect
            tmp_pdf = _download_to_temp(download_url)
            if not tmp_pdf:
                continue
            temp_files_to_cleanup.append(tmp_pdf)
            printed_date = _extract_report_date_from_pdf(tmp_pdf)
            print(f"Inspected temp file {tmp_pdf} -> printed_date = {printed_date}")
            # When a desired_date is provided, we prefer it: if printed_date matches desired_date, accept.
            if printed_date and desired_date and printed_date == desired_date:
                print("✅ Selected PDF by printed report date match to desired_date:", download_url)
                server_parsed = desired_date
                # prefer requested desired_date for filename if provided, else keep server_parsed
                if desired_date:
                    pdf_name = f"posoco_{desired_date.strftime('%d%m%Y')}.pdf"
                else:
                    pdf_name = f"posoco_{server_parsed.strftime('%d%m%Y')}.pdf"
                final_path = os.path.join(report_dir, pdf_name)
                os.replace(tmp_pdf, final_path)
                for t in temp_files_to_cleanup:
                    if os.path.exists(t) and t != final_path:
                        try:
                            os.remove(t)
                        except Exception:
                            pass
                return final_path
            # If no desired_date given, keep original behaviour (accept printed_date == today)
            if printed_date and (printed_date == local_today or printed_date == utc_today) and not desired_date:
                print("✅ Selected PDF by printed report date match (today):", download_url)
                server_parsed = printed_date
                if desired_date:
                    pdf_name = f"posoco_{desired_date.strftime('%d%m%Y')}.pdf"
                else:
                    pdf_name = f"posoco_{server_parsed.strftime('%d%m%Y')}.pdf"
                final_path = os.path.join(report_dir, pdf_name)
                os.replace(tmp_pdf, final_path)
                for t in temp_files_to_cleanup:
                    if os.path.exists(t) and t != final_path:
                        try:
                            os.remove(t)
                        except Exception:
                            pass
                return final_path

        # If we reach here no candidate matched printed date exactly.
        # Clean up temp files (we'll re-download the final chosen one normally)
        for t in temp_files_to_cleanup:
            if os.path.exists(t):
                try:
                    os.remove(t)
                except Exception:
                    pass

        # Fallback: choose the most-recent item (same as before)
        def file_date_key(item):
            parsed = infer_date_safe(item)
            if parsed:
                return parsed
            return datetime(1970, 1, 1)
        pdf_files_sorted = sorted(pdf_files, key=file_date_key, reverse=True)
        latest_item = pdf_files_sorted[0]
        file_path_rel = latest_item.get("FilePath") or latest_item.get("Path") or latest_item.get("Filepath")
        if not file_path_rel:
            print("⚠️ Missing FilePath for chosen PDF")
            return None

        server_date = file_date_key(latest_item)
        # prefer requested desired_date for filename if provided
        if desired_date:
            pdf_name = f"posoco_{desired_date.strftime('%d%m%Y')}.pdf"
        else:
            if server_date and getattr(server_date, "year", 0) > 1970:
                pdf_name = f"posoco_{server_date.strftime('%d%m%Y')}.pdf"
            else:
                pdf_name = f"posoco_{local_today.strftime('%d%m%Y')}.pdf"

        local_path = os.path.join(report_dir, pdf_name)
        download_url = base_url.rstrip("/") + "/" + file_path_rel.lstrip("/")
        if "?" not in download_url:
            download_url = download_url + f"?cachebust={int(datetime.now().timestamp())}"

        print(f"⬇️ Downloading final selected PDF: {download_url}")
        file_response = requests.get(download_url, stream=True, timeout=60)
        file_response.raise_for_status()
        with open(local_path, "wb") as fh:
            for chunk in file_response.iter_content(1024):
                fh.write(chunk)
        print(f"✅ Saved latest PDF: {local_path}")
        return local_path

    except requests.exceptions.RequestException as e:
        print(f"❌ An error occurred during download: {e}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error in fetch_latest_pdf: {e}")
        return None

# --- THIS FUNCTION HAS BEEN UPDATED: accepts desired_date but DOES NOT CHANGE TABLE SELECTION LOGIC ---
def extract_tables_from_pdf(pdf_file, report_dir, timestamp, desired_date=None):
    """
    Extracts tables, renames headings, and saves as JSON.
    This version uses flexible matching to handle unpredictable keys.
    """
    # This helper function provides the flexible matching logic
    def get_short_key_simple(long_key):
        key = long_key.strip()
        
        # We check for the most specific keys first to avoid errors
        if key.startswith("Demand Met during Evening Peak"):
            return "demand_evening_peak"
        if key.startswith("Energy Shortage"):
            return "energy_shortage"
        if key.startswith("Maximum Demand Met During the Day"):
            return "max_demand_day"
        if key.startswith("Time Of Maximum Demand Met"):
            return "time_of_max_demand"
        if key.startswith("Peak Shortage"):
            return "peak_shortage"
        if key.startswith("Energy Met"):
            return "energy"
        if key.startswith("Hydro Gen"): # Must be checked before "Hydro"
            return "hydro"
        if key.startswith("Wind Gen"):
            return "wind"
        if key.startswith("Solar Gen"):
            return "solar"
        if key == "Coal":
            return "coal"
        if key == "Lignite":
            return "lignite"
        if key == "Hydro":
            return "hydro"
        if key == "Nuclear":
            return "nuclear"
        if key == "Gas, Naptha & Diesel":
            return "gas_naptha_diesel"
        if key.startswith("RES"):
            return "res_total"
        if key == "Total":
            return "total"
            
        return key # Fallback to the original key if no match is found

    try:
        tables = tabula.read_pdf(pdf_file, pages="all", multiple_tables=True, lattice=True)
    except Exception as e:
        print(f"❌ Error reading PDF with Tabula: {e}")
        tables = []

    final_json = {"POSOCO": {"posoco_table_a": [], "posoco_table_g": []}}

    # Initialize variables to hold the found tables
    table_a_df = None
    table_g_df = None

    # Loop through all extracted tables to find the ones we need
    for df in tables:
        # Skip empty or invalid dataframes
        if df.empty or len(df.columns) == 0:
            continue

        # Convert the first column to string type for reliable searching
        first_col_str = df.iloc[:, 0].astype(str)

        # Identify Table A by looking for a unique phrase in its first column
        if any("Demand Met during Evening Peak" in s for s in first_col_str):
            print("✅ Found Table A by its content.")
            table_a_df = df

        # Identify Table G by looking for "Coal", a unique keyword for the fuel table
        elif any("Coal" in s for s in first_col_str):
            print("✅ Found Table G by its content.")
            table_g_df = df

        # If we have found both tables, we can stop searching
        if table_a_df is not None and table_g_df is not None:
            break

    # Process Table A if it was found
    if table_a_df is not None:
        table_a_df = table_a_df.set_index(table_a_df.columns[0]).dropna(how='all')
        table_a_dict = {}
        for original_key, row in table_a_df.iterrows():
            clean_key = ' '.join(str(original_key).replace('\r', ' ').split())
            short_key = get_short_key_simple(clean_key) # Use the new simple function
            table_a_dict[short_key] = row.dropna().to_dict()
        final_json["POSOCO"]["posoco_table_a"].append(table_a_dict)

    # Process Table G if it was found
    if table_g_df is not None:
        table_g_df = table_g_df.set_index(table_g_df.columns[0]).dropna(how='all')
        table_g_dict = {}
        for original_key, row in table_g_df.iterrows():
            clean_key = str(original_key).strip()
            short_key = get_short_key_simple(clean_key) # Use the new simple function
            table_g_dict[short_key] = row.dropna().to_dict()
        final_json["POSOCO"]["posoco_table_g"].append(table_g_dict)

    # Check if BOTH tables were not found, and if so, use the empty template.
    if table_a_df is None and table_g_df is None:
        final_json = {
            "POSOCO": {
                "posoco_table_a": [{"demand_evening_peak": None, "peak_shortage": None, "energy": None, "hydro": None, "wind": None, "solar": None, "energy_shortage": None, "max_demand_day": None, "time_of_max_demand": None}],
                "posoco_table_g": [{"coal": None, "lignite": None, "hydro": None, "nuclear": None, "gas_naptha_diesel": None, "res_total": None, "total": None}]
            }
        }
        print("⚠️ No valid tables found in PDF. Using empty template.")

    # Save the final JSON to a file
    # Prefer the requested desired_date for JSON filename; fallback to now()
    if desired_date:
        if isinstance(desired_date, datetime):
            date_compact = desired_date.strftime('%d%m%Y')
        else:
            # assume date-like (datetime.date)
            date_compact = desired_date.strftime('%d%m%Y')
    else:
        date_compact = datetime.now().strftime('%d%m%Y')

    json_name = f"posoco_{date_compact}.json"
    output_json = os.path.join(report_dir, json_name)

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(final_json, f, indent=4, ensure_ascii=False)

    print(f"✅ JSON with shortened keys saved successfully at: {output_json}")

    return final_json

def save_to_db(final_json):
    """Saves the processed JSON data to the Django database."""
    today = datetime.now().date()

    try:
        # Save data from Table A
        table_a_data = final_json.get("POSOCO", {}).get("posoco_table_a", [])
        if table_a_data and table_a_data[0]:
            for category, values in table_a_data[0].items():
                if values is None or not isinstance(values, dict):
                    continue
                if all(v is None for v in values.values()):
                    continue
                PosocoTableA.objects.update_or_create(
                    category=category,
                    report_date=today,
                    defaults={
                        'nr': values.get("NR"),
                        'wr': values.get("WR"),
                        'sr': values.get("SR"),
                        'er': values.get("ER"),
                        'ner': values.get("NER"),
                        'total': values.get("TOTAL"),
                    }
                )

        # Save data from Table G
        table_g_data = final_json.get("POSOCO", {}).get("posoco_table_g", [])
        if table_g_data and table_g_data[0]:
            for fuel, values in table_g_data[0].items():
                if values is None or not isinstance(values, dict):
                    continue
                if all(v is None for v in values.values()):
                    continue
                PosocoTableG.objects.update_or_create(
                    fuel_type=fuel,
                    report_date=today,
                    defaults={
                        'nr': values.get("NR"),
                        'wr': values.get("WR"),
                        'sr': values.get("SR"),
                        'er': values.get("ER"),
                        'ner': values.get("NER"),
                        'all_india': values.get("All India"),
                        'share_percent': values.get("% Share"),
                    }
                )
        print("✅ Data saved to database successfully")
    except Exception as e:
        print(f"❌ An error occurred while saving to the database: {e}")

# --- Django Management Command ---
class Command(BaseCommand):
    help = "Downloads the latest NLDC PSP PDF, extracts key tables with shortened headings, and saves them to a file and the database."

    def add_arguments(self, parser):
        """
        Accept --date in YYYY-MM-DD or DD-MM-YYYY (or many other formats parsed by _parse_date_from_string).
        If omitted, the command will use today's date.
        """
        parser.add_argument(
            '--date',
            dest='date',
            required=False,
            help='Target report date to fetch (formats: YYYY-MM-DD, DD-MM-YYYY, DDMMYYYY, etc.). If omitted, uses today.'
        )

    def handle(self, *args, **options):
        self.stdout.write("🚀 Starting POSOCO report download and processing...")
        # Parse target date from --date if passed
        raw_date = options.get('date')
        if raw_date:
            parsed_dt = _parse_date_from_string(raw_date)
            if parsed_dt is None:
                self.stdout.write(self.style.ERROR(f"❌ Could not parse date passed: {raw_date}"))
                return
            target_date = parsed_dt.date() if isinstance(parsed_dt, datetime) else parsed_dt
        else:
            target_date = datetime.now().date()

        # Make report_dir including target_date so folder names reflect requested date
        report_dir, timestamp = make_report_dir(SAVE_DIR, desired_date=target_date)
        # pass desired_date to fetch_latest_pdf (only for filename purposes)
        pdf_path = fetch_latest_pdf(API_URL, BASE_URL, payload, report_dir, timestamp, desired_date=target_date)

        if pdf_path:
            # pass desired_date to extract_tables_from_pdf so JSON filename uses target_date
            final_json = extract_tables_from_pdf(pdf_path, report_dir, timestamp, desired_date=target_date)
            if final_json and (final_json["POSOCO"]["posoco_table_a"] or final_json["POSOCO"]["posoco_table_g"]):
                save_to_db(final_json)
            else:
                self.stdout.write(self.style.WARNING("Could not extract any data from the PDF to save."))
        else:
            self.stdout.write(self.style.ERROR("Failed to download PDF. Aborting process."))

        self.stdout.write(self.style.SUCCESS("✅ Process finished."))
