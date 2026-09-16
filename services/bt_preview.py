import time, shutil
from pathlib import Path
from flask import url_for
from .bt_config import SCAN_PREVIEW, PREVIEW_OUT, STATIC_PREV, LOCAL_TEMPLATE_ROOT

def write_preview_trigger(template, pn, wo_tag, rev_tag, base="preview"):
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<XMLScript Version="2.0" Name="PreviewJob">
  <Command Name="GeneratePreview">
    <ExportPrintPreviewToImage ReturnImageInResponse="false">
      <Format>{Path(LOCAL_TEMPLATE_ROOT) / template}</Format>
      <Folder>{PREVIEW_OUT}</Folder>
      <FileNameTemplate>Preview_{base}.png</FileNameTemplate>
      <NamedSubString Name="PN"><Value>{pn}</Value></NamedSubString>
      <NamedSubString Name="WO"><Value>{wo_tag}</Value></NamedSubString>
      <NamedSubString Name="REV"><Value>{rev_tag}</Value></NamedSubString>
    </ExportPrintPreviewToImage>
  </Command>
</XMLScript>"""
    (SCAN_PREVIEW / f"{base}.xml").write_text(xml, encoding="utf-8")

def wait_and_copy_preview(base="preview"):
    pattern = f"Preview_{base}*.png"
    found = None
    for _ in range(40):
        matches = list(PREVIEW_OUT.glob(pattern))
        if matches:
            found = max(matches, key=lambda f: f.stat().st_mtime)
            break
        time.sleep(0.5)
    if not found:
        return None
    STATIC_PREV.mkdir(parents=True, exist_ok=True)
    target = STATIC_PREV / found.name
    shutil.copy(found, target)
    return f"previews/{target.name}"
