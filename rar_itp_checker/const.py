DOMAIN = "rar_itp_checker"  # integration domain used in HA internals

BASE_URL = "https://prog.rarom.ro/rarpol/rarpol.asp"  # RAR query page

# How often to re-check ITP (in hours) – 720h = 30 days
DEFAULT_SCAN_INTERVAL = 720

# DEFAULT URL of your Tesseract HTTP API /ocr/file endpoint.
# This is just a fallback; the real URL is built from the "tesseract_ip"
# field in the config entry (see sensor.py).
#
# You can leave this as-is; if "tesseract_ip" is empty, the integration
# will fall back to this value.
DEFAULT_LOCAL_OCR_API_URL = "http://127.0.0.1:8000/ocr/file?lang=eng"
