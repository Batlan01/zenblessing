from pathlib import Path
import os

# Hálózati útvonalak – nálatok működő utak
XML_PRIMARY_PATH = Path(r"\\10.10.2.15\Users\ntrencik\Documents\BarTender\Integrations\Codes\prompt3.xml")
XML_SCAN_FOLDER = Path(r"\\10.10.2.15\Users\ntrencik\Documents\BarTender\Integrations\Scan")
XML_SCAN_FILE   = XML_SCAN_FOLDER / "file.xml"

TEMPLATE_DIR = Path(r"\\10.10.2.15\Main Silcotec Folder\Engineering Dept\Bartender Templates")

# Preview
SCAN_PREVIEW = Path(r"\\10.10.2.15\Users\ntrencik\Documents\BarTender\Integrations\ScanPreview")
PREVIEW_OUT  = Path(r"\\10.10.2.15\Users\ntrencik\Documents\BarTender\Integrations\Previews")
STATIC_PREV  = Path("static/previews")
for p in (XML_SCAN_FOLDER, SCAN_PREVIEW, STATIC_PREV):
    os.makedirs(p, exist_ok=True)

# BarTender lokális template gyökér (ha kell teljes elérési út a .btw-hez)
LOCAL_TEMPLATE_ROOT = r"C:\Users\ntrencik\Documents\BarTender\Integrations\Templates"
