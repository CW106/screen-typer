"""Interface language selection; OCR text and saved settings are language-independent."""
import sys

ENGLISH_UI = "--english" in sys.argv


def tr(chinese: str, english: str) -> str:
    return english if ENGLISH_UI else chinese
