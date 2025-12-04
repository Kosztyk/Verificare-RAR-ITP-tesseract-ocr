import asyncio
import io

import pytesseract
from PIL import Image, ImageFilter, ImageOps, ImageStat

DIGITS_ONLY_CFG = "--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789"


def _clean(img: Image.Image) -> Image.Image:
    """Aggressively denoise and threshold the CAPTCHA image for OCR."""

    img = ImageOps.grayscale(img)
    img = ImageOps.autocontrast(img, cutoff=5)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = img.filter(ImageFilter.GaussianBlur(radius=0.5))

    # Adaptive threshold based on image brightness
    mean = ImageStat.Stat(img).mean[0]
    threshold = max(100, min(170, int(mean)))
    img = img.point(lambda p: 255 if p > threshold else 0, "1")

    return img.resize((img.width * 2, img.height * 2), Image.LANCZOS)


async def solve_captcha_image(raw: bytes) -> str:
    """Local fallback OCR using pytesseract.

    NOTE: This is *not* used in Home Assistant by default anymore because
    the HA container usually does not have the system `tesseract` binary
    installed. We keep it here for advanced users who may run this code
    in a full Python environment with Tesseract available.

    If Tesseract is missing, this function returns an empty string instead
    of raising TesseractNotFoundError.
    """
    def _ocr() -> str:
        img = Image.open(io.BytesIO(raw))
        img = _clean(img)
        try:
            return pytesseract.image_to_string(img, config=DIGITS_ONLY_CFG).strip()
        except pytesseract.TesseractNotFoundError:
            # Graceful fallback when the system tesseract binary is not installed.
            return ""

    return await asyncio.to_thread(_ocr)
