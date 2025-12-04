"""RAR ITP Checker with Multiple Sensors."""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, date

import aiohttp
from bs4 import BeautifulSoup
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import slugify

from .const import (
    DOMAIN,                    # integration domain
    BASE_URL,                  # RAR base URL
    DEFAULT_SCAN_INTERVAL,     # scan interval in hours
    DEFAULT_LOCAL_OCR_API_URL, # default Tesseract URL from const.py
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(hours=DEFAULT_SCAN_INTERVAL)

# Mutable URL that will be overridden based on the config entry.
# Start with the default from const.py.
LOCAL_OCR_API_URL = DEFAULT_LOCAL_OCR_API_URL

# Month mapping for Romanian date parsing
MONTH_MAP = {
    "ian": "01",
    "feb": "02",
    "mar": "03",
    "apr": "04",
    "mai": "05",
    "iun": "06",
    "iul": "07",
    "aug": "08",
    "sept": "09",
    "oct": "10",
    "nov": "11",
    "dec": "12",
}


class OCRAPIError(Exception):
    """Custom exception for OCR API errors."""


def save_captcha_image(image_bytes: bytes, vin: str, attempt: int) -> None:
    """Save CAPTCHA image under /config/www/rar_itp_captchas for debugging."""
    try:
        base_dir = "/config/www/rar_itp_captchas"
        os.makedirs(base_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_vin = re.sub(r"[^A-Za-z0-9]", "_", vin)
        filename = f"captcha_{safe_vin}_attempt{attempt}_{ts}.png"
        path = os.path.join(base_dir, filename)
        with open(path, "wb") as f:
            f.write(image_bytes)
        _LOGGER.warning("Saved CAPTCHA image to %s", path)
    except Exception as e:
        _LOGGER.error("Failed to save CAPTCHA image: %s", e)


async def solve_captcha_with_local_api(image_bytes: bytes) -> str:
    """
    Solve CAPTCHA using ONLY the local Tesseract HTTP API.

    LOCAL_OCR_API_URL must point to something like:
      http://192.168.68.144:8000/ocr/file?lang=eng

    The endpoint is expected to return JSON like:
      { "text": "1234", "length": 4, ... }
    """
    try:
        timeout = aiohttp.ClientTimeout(total=15)

        # Build final URL: always enforce expected_length=4
        if "?" in LOCAL_OCR_API_URL:
            url = f"{LOCAL_OCR_API_URL}&expected_length=4"
        else:
            url = f"{LOCAL_OCR_API_URL}?expected_length=4"

        async with aiohttp.ClientSession(timeout=timeout) as session:
            form = aiohttp.FormData()
            form.add_field(
                "file",
                image_bytes,
                filename="captcha.png",
                content_type="image/png",
            )

            try:
                async with session.post(url, data=form) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        msg = f"OCR API HTTP {resp.status}: {text[:200]}"
                        _LOGGER.warning(msg)
                        raise OCRAPIError(msg)

                    data = await resp.json()
                    raw_text = str(data.get("text", "")).strip()
                    if not raw_text:
                        _LOGGER.warning(
                            "OCR API returned empty text, raw response: %s", data
                        )
                        raise OCRAPIError("OCR API returned empty text")

                    digits_only = re.sub(r"\D", "", raw_text)

                    # Log what Tesseract actually returned
                    _LOGGER.warning(
                        "Local OCR API result: raw=%r, digits=%r, length=%d",
                        raw_text,
                        digits_only,
                        len(digits_only),
                    )

                    if not re.fullmatch(r"\d{1,6}", digits_only):
                        raise OCRAPIError(
                            f"Invalid CAPTCHA format from OCR API: raw={raw_text!r}, digits={digits_only!r}"
                        )

                    # For RAR we expect exactly 4 digits; if more, take first 4.
                    if len(digits_only) >= 4:
                        digits_only = digits_only[:4]

                    return digits_only

            except asyncio.TimeoutError:
                _LOGGER.warning("Local OCR API timeout when calling %s", url)
                raise OCRAPIError("OCR API timeout")

    except Exception as e:
        _LOGGER.warning("OCR processing via local API failed: %s", str(e))
        raise OCRAPIError("OCR processing failed") from e


def _build_form_data_from_page(
    soup: BeautifulSoup, vin: str, captcha_code: str
) -> tuple[str, dict]:
    """
    Build POST data from the actual form on the RAR page.

    - Reads the <form> and all <input name="..."> values
    - Overrides only VIN + CAPTCHA fields
    - Returns (post_url, form_data)
    """
    form_el = soup.find("form", attrs={"name": "frm"})
    if not form_el:
        form_el = soup.find("form")
    if not form_el:
        raise UpdateFailed("Unable to find form element on RAR page")

    # Determine POST URL
    action = form_el.get("action") or ""
    if action.startswith("http"):
        post_url = action
    else:
        if "#" in action:
            action_clean = action.split("#", 1)[0]
        else:
            action_clean = action

        if action_clean:
            if action_clean.startswith("/"):
                post_url = f"https://prog.rarom.ro{action_clean}"
            else:
                base_root = BASE_URL.rsplit("/", 1)[0]
                post_url = f"{base_root}/{action_clean}"
        else:
            post_url = BASE_URL

    inputs = form_el.find_all("input")
    form_data: dict[str, str] = {}

    for inp in inputs:
        name = inp.get("name")
        if not name:
            continue
        value = inp.get("value", "")
        form_data[name] = value

    # Override VIN field
    if "nr_id" in form_data:
        form_data["nr_id"] = vin.upper()
    else:
        form_data["nr_id"] = vin.upper()

    # Override CAPTCHA field – NEW: verif_cod (current field name on site)
    if "verif_cod" in form_data:
        form_data["verif_cod"] = captcha_code
    elif "antirobot" in form_data:
        # old name, keep for backward compatibility
        form_data["antirobot"] = captcha_code
    else:
        # fallback if form has changed
        form_data["verif_cod"] = captcha_code

    # Submit button
    if "trimite" in form_data and not form_data["trimite"]:
        form_data["trimite"] = "Caută"

    _LOGGER.debug(
        "Prepared form_data keys for POST: %s (post_url=%s)",
        list(form_data.keys()),
        post_url,
    )

    return post_url, form_data


async def fetch_itp(vin: str) -> dict:
    """Fetch ITP data from RAR site with robust CAPTCHA handling."""
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {
        "User-Agent": "Mozilla/5.0 (HA RAR ITP Checker)",
        "Referer": BASE_URL,
        "Origin": "https://prog.rarom.ro",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            _LOGGER.info("Starting ITP check for VIN: %s", vin)

            result_text = ""

            # CAPTCHA handling with retries
            for attempt in range(1, 4):  # attempts 1..3
                try:
                    # 1) Load initial page to get a CAPTCHA image and the form
                    async with session.get(BASE_URL) as response:
                        if response.status != 200:
                            raise UpdateFailed(
                                f"Initial request failed: HTTP {response.status}"
                            )
                        html = await response.text()

                    soup = BeautifulSoup(html, "html.parser")

                    captcha_img = soup.find("img", id="imgVerf")
                    if not captcha_img or not captcha_img.get("src"):
                        _LOGGER.debug("CAPTCHA HTML: %s", str(captcha_img))
                        raise UpdateFailed("CAPTCHA image not found in page")

                    captcha_src = captcha_img["src"]
                    if captcha_src.startswith("http"):
                        captcha_url = captcha_src
                    else:
                        captcha_url = (
                            f"https://prog.rarom.ro/rarpol/{captcha_src.lstrip('/')}"
                        )

                    _LOGGER.debug("Downloading CAPTCHA from: %s", captcha_url)
                    async with session.get(captcha_url) as cap_resp:
                        if cap_resp.status != 200:
                            raise UpdateFailed(
                                f"CAPTCHA download failed: HTTP {cap_resp.status}"
                            )
                        captcha_content = await cap_resp.read()

                    # Save captcha locally for debugging
                    save_captcha_image(captcha_content, vin, attempt)

                    # 2) Solve CAPTCHA via local Tesseract HTTP API
                    try:
                        captcha_text = await solve_captcha_with_local_api(
                            captcha_content
                        )
                    except OCRAPIError as err:
                        _LOGGER.warning(
                            "Local OCR API failed (attempt %d): %s",
                            attempt,
                            err,
                        )
                        raise UpdateFailed(f"OCR API failed: {err}")

                    if not captcha_text:
                        raise UpdateFailed("CAPTCHA OCR returned empty result")

                    clean_captcha = re.sub(r"\D", "", captcha_text)

                    # Log what we’re about to send to RAR
                    _LOGGER.warning(
                        "Attempt %d: VIN=%s, using CAPTCHA code=%r (clean=%r)",
                        attempt,
                        vin,
                        captcha_text,
                        clean_captcha,
                    )

                    if not re.fullmatch(r"\d{4}", clean_captcha):
                        raise UpdateFailed(
                            f"Invalid CAPTCHA output after cleaning: {clean_captcha}"
                        )

                    # 3) Build form data from real page form
                    post_url, form_data = _build_form_data_from_page(
                        soup, vin, clean_captcha
                    )

                    _LOGGER.debug(
                        "Posting to %s with verif_cod=%s, nr_id=%s; all keys=%s",
                        post_url,
                        form_data.get("verif_cod"),
                        form_data.get("nr_id"),
                        list(form_data.keys()),
                    )

                    # 4) Submit form to RAR
                    async with session.post(
                        post_url, data=form_data
                    ) as result_response:
                        if result_response.status != 200:
                            raise UpdateFailed(
                                f"POST request failed: HTTP {result_response.status}"
                            )
                        result_text = await result_response.text()

                        if (
                            "codul de verificare a fost copiat incorect"
                            in result_text.lower()
                        ):
                            _LOGGER.warning(
                                "CAPTCHA validation failed on server (attempt %d) for VIN %s, code used: %s",
                                attempt,
                                vin,
                                clean_captcha,
                            )
                            # Wrong CAPTCHA → retry loop
                            raise UpdateFailed("CAPTCHA validation failed")

                        # Success – break retry loop
                        break

                except UpdateFailed as e:
                    if attempt == 3:
                        # Last attempt → bubble up
                        raise UpdateFailed(f"Failed after 3 attempts: {str(e)}")
                    _LOGGER.debug("Attempt %d failed, retrying: %s", attempt, e)
                    await asyncio.sleep(2)
                    continue

            # ---- Parse results from RAR HTML ----
            result_soup = BeautifulSoup(result_text, "html.parser")
            result_div = result_soup.find("div", id="rezbgcolor")
            content_text = (
                result_div.get_text(separator="\n", strip=True)
                if result_div
                else result_text
            )
            lower = content_text.lower()

            # Default values
            status = "Not Found"
            expiration_date = "Unknown"

            if "nu a fost găsită nicio înregistrare" not in lower:
                status = "Valid"

                # New format parsing: 'valabilă până la d-mmm-yyyy'
                if "valabilă până la" in lower:
                    try:
                        fragment = lower.split("valabilă până la", 1)[1]
                        raw_date = fragment.split()[0].strip().strip(".")
                        day, month, year = raw_date.split("-")
                        expiration_date = (
                            f"{year}-{MONTH_MAP.get(month, '01')}-{day.zfill(2)}"
                        )
                    except Exception as e:
                        _LOGGER.warning("Failed to parse expiration date: %s", e)

                # Fallback old format parsing
                elif "data expirării" in lower:
                    try:
                        node = result_soup.find(
                            text=lambda t: "Data expirării" in t
                        )
                        if node:
                            raw = node.find_next().get_text(strip=True)
                            day, month, year = raw.split(".")
                            expiration_date = f"{year}-{month}-{day}"
                    except Exception as e:
                        _LOGGER.warning("Failed to parse old-format date: %s", e)

            return {
                "vin": vin,
                "status": status,
                "expiration_date": expiration_date,
                "last_checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        except Exception as err:
            _LOGGER.error("ITP check failed for %s: %s", vin, err, exc_info=True)
            raise UpdateFailed(f"ITP check failed: {err}") from err


def calculate_days_until(expiration_date: str) -> int | None:
    """Calculate days until expiration."""
    if not expiration_date or expiration_date == "Unknown":
        return None
    try:
        exp = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        return (exp - date.today()).days
    except ValueError:
        return None


class ITPStatusSensor(CoordinatorEntity, SensorEntity):
    """ITP status sensor."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        vin = coordinator.data["vin"]
        self._attr_name = f"ITP Status {vin}"
        self._attr_unique_id = slugify(f"itp_status_{vin}")
        self._attr_icon = "mdi:car"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.coordinator.data.get("status", "unknown")

    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        return {
            "vin": self.coordinator.data.get("vin"),
            "last_checked": self.coordinator.data.get("last_checked"),
        }


class ITPExpirationDateSensor(CoordinatorEntity, SensorEntity):
    """ITP expiration date sensor."""

    _attr_device_class = "date"
    _attr_icon = "mdi:calendar-star"

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        vin = coordinator.data["vin"]
        self._attr_name = f"ITP Expiration Date {vin}"
        self._attr_unique_id = slugify(f"itp_expiration_date_{vin}")

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.coordinator.data.get("expiration_date", "Unknown")


class ITPLastCheckedSensor(CoordinatorEntity, SensorEntity):
    """Last checked timestamp sensor."""

    _attr_device_class = "timestamp"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        vin = coordinator.data["vin"]
        self._attr_name = f"ITP Last Checked {vin}"
        self._attr_unique_id = slugify(f"itp_last_checked_{vin}")

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.coordinator.data.get("last_checked")


class ITPDaysLeftSensor(CoordinatorEntity, SensorEntity):
    """Days left until ITP expiration."""

    _attr_native_unit_of_measurement = "days"
    _attr_state_class = "measurement"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        vin = coordinator.data["vin"]
        self._attr_name = f"ITP Days Left {vin}"
        self._attr_unique_id = slugify(f"itp_days_left_{vin}")

    @property
    def native_value(self):
        """Return the native value of the sensor."""
        exp_date = self.coordinator.data.get("expiration_date")
        return calculate_days_until(exp_date)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up sensors from config entry with improved error handling."""
    # VIN is stored in config_entry.data from the initial config_flow
    vin = config_entry.data["vin"]  # string VIN for this config entry

    # ------------------------------------------------------------------
    # Read Tesseract endpoint ONLY from "tesseract_ip":
    #   1. config_entry.options["tesseract_ip"] (set via Configure / OptionsFlow)
    #   2. config_entry.data["tesseract_ip"]    (set during initial config flow)
    # If both are empty → we'll fall back to DEFAULT_LOCAL_OCR_API_URL.
    # ------------------------------------------------------------------
    tesseract_ip = (
        config_entry.options.get("tesseract_ip", "").strip()
        or config_entry.data.get("tesseract_ip", "").strip()
    )

    # effective_tesseract is what we'll actually use to build the URL
    effective_tesseract = tesseract_ip

    # GLOBAL because solve_captcha_with_local_api() reads this module-level var.
    global LOCAL_OCR_API_URL

    if effective_tesseract:
        # If user provided a full URL (starts with http:// or https://), use it as-is.
        if effective_tesseract.startswith("http://") or effective_tesseract.startswith("https://"):
            LOCAL_OCR_API_URL = effective_tesseract
        else:
            # Otherwise assume it's just an IP/hostname and build the full URL.
            # Example: "192.168.68.144" → "http://192.168.68.144:8000/ocr/file?lang=eng"
            LOCAL_OCR_API_URL = (
                f"http://{effective_tesseract}:8000/ocr/file?lang=eng"
            )
        _LOGGER.warning("Using Tesseract OCR URL: %s", LOCAL_OCR_API_URL)
    else:
        # Nothing set in options or data → fall back to default from const.py
        LOCAL_OCR_API_URL = DEFAULT_LOCAL_OCR_API_URL
        _LOGGER.warning(
            "No tesseract_ip configured; falling back to default OCR URL: %s",
            LOCAL_OCR_API_URL,
        )

    # This function is called by the DataUpdateCoordinator on a schedule
    async def async_update_data():
        """Wrap the fetch with retry logic."""
        for attempt in range(3):
            try:
                # fetch_itp now only needs the VIN; OCR config is global (LOCAL_OCR_API_URL)
                return await fetch_itp(vin)
            except UpdateFailed as e:
                if attempt == 2:  # Last attempt → re-raise
                    raise
                _LOGGER.debug("Attempt %d failed, retrying: %s", attempt + 1, e)
                await asyncio.sleep(2)
                continue

    # Coordinator is responsible for scheduling and caching the data updates
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{vin}",          # coordinator name in logs
        update_method=async_update_data, # function defined above
        update_interval=SCAN_INTERVAL,   # timedelta from const.py
    )

    try:
        # First refresh – actually fetch data once before adding the sensors
        await coordinator.async_config_entry_first_refresh()
    except Exception as ex:
        _LOGGER.error("Failed to setup RAR ITP Checker: %s", str(ex))
        # Tell HA to retry the setup later instead of marking it failed forever
        raise ConfigEntryNotReady from ex

    # Store coordinator by VIN in hass.data so services / other parts can access it
    hass.data.setdefault(DOMAIN, {})[vin] = {"coordinator": coordinator}

    # Create all sensors bound to this coordinator
    sensors = [
        ITPStatusSensor(coordinator),
        ITPExpirationDateSensor(coordinator),
        ITPLastCheckedSensor(coordinator),
        ITPDaysLeftSensor(coordinator),
    ]
    # Register the entities in Home Assistant
    async_add_entities(sensors, True)
