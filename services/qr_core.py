# services/qr_core.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import mysql.connector
import qrcode
from docx import Document

from services.dbpool import get_pooled_connection
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt


class QRProgramCore:
    """
    Az eredeti Tkinter-es logika UI nélkül, Flask backendhez.
    - process_file(input_path, enable_word_com=False, preview=False) a publikus belépő.
    - preview=True esetén NEM ír DB-be; a beszúrandó sorokat self.preview_rows gyűjti.
    - A DOCX mentés/QR generálás az eredeti elvek szerint történik (Word megnyitás nélkül).
    """

    def __init__(self, db_cfg: Dict[str, str] | None = None):
        self.prog_version = "v2.1.1"
        self.prog_name = f"QR Injector - {self.prog_version}"

        self.db_cfg = db_cfg or {
            "host": "10.10.2.15",
            "user": "root",
            "password": "admin321",
            "database": "paperless",
        }

        # per-object DB kapcsolat a közös poolból (a hívó a close()-zal adja vissza)
        # Szándékosan NEM tartunk fenn kérés-hosszú kapcsolatot. Korábban itt
        # nyílt egy pooled kapcsolat, amit csak a _tdump_row() használt, és amit
        # a kérés végéig fogva tartottunk – miközben a master_lookup_mains() és
        # a flush_pending_rows() közben kapcsolatokat kért és adott vissza a
        # poolba. Ez a hosszú életű kapcsolat futott "Invalid result" hibára,
        # míg a rövid életűek soha. Mostantól minden lekérdezés ugyanazt a
        # kérj–használd–add vissza mintát követi.
        self.conn = None
        self.cursor = None

        # kérésenként EGYEDI temp könyvtárak: párhuzamos feldolgozások nem
        # törölhetik egymás QR képeit (korábban közös ./temp és ./temp_recursive volt)
        self.temp_dir = tempfile.mkdtemp(prefix="qr_main_")
        self.temp_recursive_dir = tempfile.mkdtemp(prefix="qr_sub_")

        # t_dump sor cache PN-enként (None = a PN nincs a táblában)
        self._tdump_cache: Dict[str, Optional[tuple]] = {}
        # kötegelt workorders írás: a sorok itt gyűlnek, a flush_pending_rows() írja ki
        self._pending_rows: List[Dict[str, Any]] = []

        # állapot
        self.processed_pns: List[str] = []
        self.final_results: List[str] = []
        self.qr_codes: Dict[str, Any] = {}
        self.qr_locations: List[str] = []
        self.document: Document = Document()
        self.i = 0
        self.current_qty_value = ""
        self.visited = set()
        self.inserted_pictures = set()
        self.image_tracking: List[str] = []
        self.visited_parts = set()
        self.found_images: List[str] = []
        self.found_names: List[str] = []
        self.unique_paths: List[str] = []
        self.wo_pn_list: List[Tuple[str, str]] = []
        self.wo_qty_mapping: Dict[str, str] = {}

        # preview mód
        self.preview: bool = False
        self.preview_rows: List[Dict[str, Any]] = []

    def close(self) -> None:
        """DB kapcsolat visszaadása a poolba. A route hívja a kérés végén."""
        try:
            if self.cursor is not None:
                self.cursor.close()
        except Exception:
            pass
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        self.cursor = None
        self.conn = None

        # saját temp könyvtárak eltakarítása hiba esetén is
        for d in (self.temp_dir, self.temp_recursive_dir):
            try:
                if d and os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass

    # -------------------- Preview API --------------------

    def set_preview(self, flag: bool):
        self.preview = bool(flag)
        self.preview_rows.clear()

    def get_preview_rows(self) -> List[Dict[str, Any]]:
        return self.preview_rows

    # -------------------- Segédek --------------------

    def _to_int_or_1(self, x):
        try:
            s = str(x).strip()
            return int(s) if s else 1
        except Exception:
            return 1

    def _normalize_qty(self, q):
        try:
            qf = float(q)
            return int(qf) if qf.is_integer() else qf
        except Exception:
            return None

    def is_simple(self, WO, wo_list, sub_list, parts):
        for i in range(len(wo_list)):
            if WO == wo_list[i]:
                if i < len(sub_list) and sub_list[i] is not None:
                    subperkeles = str(sub_list[i]).split("|")
                    return "Multi", parts[i], subperkeles
                else:
                    return "Simple", parts[i], "##"
        return "Simple", None, "##"

    # a t_dump-ból mindig ugyanezt a 10 oszlopot kérjük le, egyszer PN-enként
    _TDUMP_COLS = (
        "SUBS", "`SUBS QTYS`", "`GROUP`", "`PROD.REV`", "CELL",
        "`CURRENT ECN`", "WCUT", "PROD", "TEST", "FIQC",
    )

    def _tdump_row(self, pn) -> Optional[tuple]:
        """Egy PN t_dump sora cache-elve. Azonos PN-re csak egyszer megy DB-be.

        A LIMIT 1 azért kell, mert egy PART.NBR a t_dump-ban többször is
        szerepelhet – a dump nem garantálja az egyediséget. Nélküle a fetchone()
        után olvasatlan sorok maradtak a kapcsolaton, és a következő lekérdezés
        elszállt ("Invalid result").
        """
        if pn in self._tdump_cache:
            return self._tdump_cache[pn]

        conn = get_pooled_connection()
        cursor = conn.cursor(buffered=True)
        try:
            cursor.execute(
                f"""
                SELECT {', '.join(self._TDUMP_COLS)}
                FROM t_dump
                WHERE `PART.NBR` = %s
                LIMIT 1;
            """,
                (pn,),
            )
            row = cursor.fetchone()
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()  # vissza a poolba minden ágon
            except Exception:
                pass

        self._tdump_cache[pn] = row
        return row

    def fetch_additional_data(self, pn):
        row = self._tdump_row(pn)
        if row:
            # FIQC, TEST, PROD, WCUT
            return (row[9], row[8], row[7], row[6])
        return (None, None, None, None)

    # -------------------- Parser --------------------

    def data_extract(self, path: str):
        with open(path, "r", encoding="latin-1", errors="ignore") as openfile:
            WO_S: List[str] = []
            parts: List[str] = []
            just_wo: List[str] = []
            master_pn_list: List[str] = []

            current_wo_data = ""
            WO_SET = 0
            i = 0

            for line in openfile:
                if "WORK ORDER NBR:" in line and WO_SET == 0:
                    temp = " ".join(line.split()).split(" ")
                    WO = temp[3]
                    GRP = temp[13]
                    current_wo_data = f"WO-{WO}|GRP-{GRP}|PN-"
                    WO_SET = 1
                    just_wo.append(WO)

                elif "PART NUMBER: " in line and WO_SET == 1:
                    temp = " ".join(line.split()).split(" ")
                    PN = temp[2]
                    master_pn_list.append(PN)
                    DD = temp[5]
                    REV = temp[8]
                    try:
                        CLL = temp[12]
                    except Exception:
                        CLL = "##"
                    MLT = f"M{i}"
                    i += 1
                    current_wo_data += (
                        f"{PN}|MASTER_PN-{PN}|DUE_DATE-{DD}|REV-{REV}|CLL-{CLL}|MLT-{MLT}|QTY-"
                    )
                    WO_SET = 2
                    parts.append(PN)

                elif "DESCRIPTION:" in line and WO_SET == 2:
                    temp = " ".join(line.split()).split(":")
                    BCH = temp[2].replace(" ECN NUMBER ", "").strip()
                    ECN = temp[3].replace(" CUSTOMER ", "").strip()
                    current_wo_data = current_wo_data + BCH + "|ECN-" + ECN + "|ST-"
                    WO_SET = 3

                elif "020  PROD  MANU PRODUCTION" in line and WO_SET == 3:
                    temp = " ".join(line.split()).split(" ")
                    ST = temp[4]
                    STT = temp[7]
                    current_wo_data = current_wo_data + ST + "|STT-" + STT
                    WO_SET = 4

                elif "BATCH QTY:" in line and WO_SET == 4:
                    parts_ = line.split("BATCH QTY:")
                    if len(parts_) > 1:
                        qty_part = parts_[1].split("ECN NUMBER")[0].strip()
                        QTY = qty_part.split()[0].strip()
                    else:
                        QTY = "##"
                    current_wo_data = current_wo_data + QTY
                    self.wo_qty_mapping[just_wo[-1]] = QTY
                    WO_SET = 5

                elif "INITIAL | DATE | TYPE | QTY  | PASS | FAIL " in line and WO_SET == 5:
                    current_wo_data = current_wo_data + "##|STT-##"
                    WO_S.append(current_wo_data)
                    WO_SET = 0

        return WO_S, parts, just_wo, master_pn_list

    # -------------------- DB írás (erase-then-insert) + preview --------------------

    def insert_into_db(self, wo_data: Dict[str, Any]):
        vals = {
            "WO": wo_data.get("WO"),
            "PN": wo_data.get("PN"),
            "QTY": wo_data.get("QTY"),
            "MLT_STATUS": wo_data.get("MLT_STATUS", "N/A"),
            "TIME_FIQC": wo_data.get("TIME_FIQC", "N/A"),
            "TIME_TEST": wo_data.get("TIME_TEST", "N/A"),
            "TIME_PROD": wo_data.get("TIME_PROD", "N/A"),
            "TIME_WCUT": wo_data.get("TIME_WCUT", "N/A"),
            "ECN": wo_data.get("ECN", "N/A"),
            "REV": wo_data.get("REV", "N/A"),
            "CLL": wo_data.get("CLL", "N/A"),
            "GRP": wo_data.get("GRP", "N/A"),
            "START_DATE": wo_data.get("START_DATE", "N/A"),
            "DUE_DATE": wo_data.get("DUE_DATE", "N/A"),
            "MASTER_PN": wo_data.get("MASTER_PN"),
            "HIERARCHY": wo_data.get("HIERARCHY", "N/A"),
        }

        # Preview: csak gyűjtjük, nem írunk DB-be
        if self.preview:
            self.preview_rows.append(dict(vals))
            return

        # nem írunk soronként DB-be (kapcsolat + COUNT + DELETE + INSERT + COMMIT
        # minden sorra nagyon lassú volt) – a flush_pending_rows() írja ki egyben
        self._pending_rows.append(vals)

    def flush_pending_rows(self) -> None:
        """Az összegyűjtött workorders sorok kiírása egy kapcsolaton, egy commit-tal."""
        if not self._pending_rows:
            return

        # (WO, PN, HIERARCHY) kulcsonként az utolsó sor nyer – ez felel meg az
        # eredeti soronkénti erase-then-insert viselkedésnek
        deduped: Dict[tuple, Dict[str, Any]] = {}
        for row in self._pending_rows:
            deduped[(row["WO"], row["PN"], row["HIERARCHY"])] = row
        self._pending_rows = []

        keys = list(deduped.keys())
        rows = [
            (
                r["WO"],
                r["PN"],
                r["QTY"],
                r["MLT_STATUS"],
                r["TIME_FIQC"],
                r["TIME_TEST"],
                r["TIME_PROD"],
                r["TIME_WCUT"],
                r["ECN"],
                r["REV"],
                r["CLL"],
                r["GRP"],
                r["START_DATE"],
                r["DUE_DATE"],
                r["MASTER_PN"],
                r["HIERARCHY"],
            )
            for r in deduped.values()
        ]

        # szándékosan NEM executemany: a mysql-connector-python régebbi
        # verzióiban az executemany INSERT ága "unknown encoding: utf8mb4"
        # hibát dob; többsoros VALUES-szal, sima execute-tal írunk adagonként
        CHUNK = 200

        conn = get_pooled_connection()
        cursor = conn.cursor()
        try:
            for i in range(0, len(keys), CHUNK):
                chunk = keys[i:i + CHUNK]
                placeholders = ", ".join(["(%s,%s,%s)"] * len(chunk))
                cursor.execute(
                    f"""
                    DELETE FROM workorders
                    WHERE (WO, PN, HIERARCHY) IN ({placeholders})
                """,
                    [v for k in chunk for v in k],
                )
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i:i + CHUNK]
                placeholders = ", ".join(
                    ["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(chunk)
                )
                cursor.execute(
                    f"""
                    INSERT INTO workorders
                    (WO, PN, QTY, MLT_STATUS, TIME_FIQC, TIME_TEST, TIME_PROD, TIME_WCUT,
                     ECN, REV, CELL, `GROUP`, START_DATE, DUE_DATE, MASTER_PN, HIERARCHY)
                    VALUES {placeholders}
                """,
                    [v for r in chunk for v in r],
                )
            conn.commit()
        finally:
            try: cursor.close()
            except Exception: pass
            try: conn.close()  # vissza a poolba minden ágon
            except Exception: pass

    # -------------------- QR generálás --------------------

    def extract_qr_data(self, updated_data: str) -> Dict[str, Any]:
        data_dict: Dict[str, Any] = {}
        for item in updated_data.split("|"):
            if "-" in item:
                key, value = item.split("-", 1)
                if key in data_dict:
                    if isinstance(data_dict[key], list):
                        data_dict[key].append(value)
                    else:
                        data_dict[key] = [data_dict[key], value]
                else:
                    data_dict[key] = value
        return data_dict

    def create_QR(self, WO_LIST: List[str], ertek: str, hierarchy_data: str = ""):
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.temp_recursive_dir, exist_ok=True)
        temp_destination = self.temp_dir
        temp_recursive_destination = self.temp_recursive_dir

        for data in WO_LIST:
            picture_name = data.split("|")
            wo_string = str(picture_name[0]).replace("WO-", "")
            pn_string = str(picture_name[2]).replace("PN-", "")

            mlt_value = None
            master_pn_value = None
            for item in picture_name:
                if item.startswith("MLT-"):
                    mlt_value = item.replace("MLT-", "")
                elif item.startswith("MASTER_PN-"):
                    master_pn_value = item.replace("MASTER_PN-", "")

            # ha volt hierarchy az inputban, azt megtartjuk
            hierarchy_data = next(
                (item.replace("HIERARCHY-", "") for item in picture_name if item.startswith("HIERARCHY-")), ""
            )

            time_fiqc, time_test, time_prod, time_wcut = self.fetch_additional_data(pn_string)

            updated_items: List[str] = []
            for item in picture_name:
                if item.startswith("MLT-"):
                    updated_items.append(f"MLT_STATUS-{mlt_value}")
                elif not item.startswith(("DD-", "ST-", "STT-", "HIERARCHY-")):
                    updated_items.append(item)

            updated_items.append(f"TIME_FIQC-{time_fiqc}")
            updated_items.append(f"TIME_TEST-{time_test}")
            updated_items.append(f"TIME_PROD-{time_prod}")
            updated_items.append(f"TIME_WCUT-{time_wcut}")

            if hierarchy_data:
                updated_items.append(f"HIERARCHY-{hierarchy_data}")
            if master_pn_value:
                updated_items.append(f"MASTER_PN-{master_pn_value}")

            updated_data = "|".join(updated_items)

            # QR kép mentés
            img = qrcode.make(updated_data)
            file_name_parts = hierarchy_data.replace(":", "").split()
            file_name = f"{wo_string}_{pn_string}{'_'.join(file_name_parts)}.jpg"
            dest = temp_destination if ertek == "sub" else temp_recursive_destination
            img.save(os.path.join(dest, file_name))

            # DB-hez dict + QTY fallback
            wo_data = self.extract_qr_data(updated_data)
            if wo_data.get("QTY") in (None, "", "N/A", "None"):
                wo_qty = self.wo_qty_mapping.get(wo_data.get("WO"))
                if wo_qty not in (None, "", "None"):
                    wo_data["QTY"] = wo_qty
            wo_data["MLT_STATUS"] = mlt_value

            # listás értékek kisimítása
            for k, v in list(wo_data.items()):
                if isinstance(v, list):
                    wo_data[k] = v[0]

            self.insert_into_db(wo_data)

        return "OK"

    # -------------------- Rekurzív SUB logika --------------------

    def query_recursive_subs(self, wo, part, current_wo, current_master_pn, hierarchy, qty):
        if part == "1543464":
            return

        if wo != current_wo:
            current_master_pn = part
            current_wo = wo

        result = self._tdump_row(part)
        if result:
            subs, subs_qtys, grp, rev, cll, ecn, wcut, prod, test, fiqc = result
        else:
            subs = subs_qtys = None
            grp = rev = cll = ecn = wcut = prod = test = fiqc = "N/A"

        hierarchy_pn = f"PN: {part}" if not hierarchy else f"{hierarchy} SUB{hierarchy.count('SUB')}: {part}"
        formatted_qty = self._normalize_qty(qty)

        target_string = (
            f"WO-{wo}|GRP-{grp}|PN-{part}|MASTER_PN-{current_master_pn}|REV-{rev}|CLL-{cll}|"
            f"QTY-{formatted_qty if formatted_qty is not None else self.wo_qty_mapping.get(wo, 'N/A')}"
            f"|ECN-{ecn}|TIME_WCUT-{wcut}|TIME_PROD-{prod}|TIME_TEST-{test}|TIME_FIQC-{fiqc}|"
            f"HIERARCHY-{hierarchy_pn}"
        )

        # SUB0 -> SUB1 normalizálás
        target_string_cleaned = re.sub(r"(HIERARCHY-PN:)[^SUB]+SUB0:", r"\1", target_string)
        target_string_cleaned = re.sub(r"SUB0:", "SUB1:", target_string_cleaned)

        # QR mentés ezen a szinten
        os.makedirs(self.temp_recursive_dir, exist_ok=True)
        qr = qrcode.make(target_string_cleaned)
        qr_filename = f"WO-{wo} HIERARCHY-{hierarchy_pn.replace(':', '').replace('|', ' ')}.jpg"
        qr.save(os.path.join(self.temp_recursive_dir, qr_filename))

        # DB insert CSAK ha nem gyökérszint (van már hierarchy lánc)
        if hierarchy:
            hierarchy_pn_cleaned = re.sub(r"(PN:)[^SUB]+SUB0:", r"\1", hierarchy_pn)
            hierarchy_pn_cleaned = re.sub(r"SUB0:", "SUB1:", hierarchy_pn_cleaned)
            wo_data = {
                "WO": wo,
                "PN": part,
                "QTY": formatted_qty if formatted_qty is not None else self.wo_qty_mapping.get(wo, "N/A"),
                "MLT_STATUS": "N/A",
                "TIME_FIQC": fiqc,
                "TIME_TEST": test,
                "TIME_PROD": prod,
                "TIME_WCUT": wcut,
                "ECN": ecn if ecn else "N/A",
                "REV": rev if rev else "N/A",
                "CLL": cll if cll else "N/A",
                "GRP": grp if grp else "N/A",
                "START_DATE": "N/A",
                "DUE_DATE": "N/A",
                "MASTER_PN": current_master_pn,
                "HIERARCHY": hierarchy_pn_cleaned,
            }
            try:
                self.insert_into_db(wo_data)
            except Exception as e:
                logging.error("DB insert error: %s", e)

        # rekurzió lefelé
        if subs and subs != "None":
            sub_pn_list = subs.split("|")
            sub_qty_list = subs_qtys.split("|") if subs_qtys else ["1"] * len(sub_pn_list)
            for sub_part, sub_qty in zip(sub_pn_list, sub_qty_list):
                new_qty = (float(qty) if qty not in (None, "", "None") else 1.0) * self._to_int_or_1(sub_qty)
                self.query_recursive_subs(wo, sub_part, current_wo, current_master_pn, hierarchy_pn, new_qty)

    def query_filtered_parts(self, filtered_wo_pn_list: List[Tuple[str, str]]):
        previous_wo = None
        previous_master_pn = None
        for wo, part in filtered_wo_pn_list:
            qty = float(self.wo_qty_mapping.get(wo, "1"))
            master_pn = part
            if wo != previous_wo:
                previous_wo = wo
                previous_master_pn = master_pn
            self.query_recursive_subs(wo, part, previous_wo, previous_master_pn, "", qty)

    # -------------------- DOCX összerakás --------------------

    def add_qr_images_to_document(self):
        # Ugrás a dokumentum végére
        self.document.add_page_break()

        # Fájlok csoportosítása WO szerint
        wo_grouped_images: Dict[str, List[str]] = {}
        if not os.path.isdir(self.temp_recursive_dir):
            return

        for file in os.listdir(self.temp_recursive_dir):
            if file.endswith(".jpg"):
                wo = file.split(" ")[0].replace("WO-", "")
                wo_grouped_images.setdefault(wo, []).append(file)

        # Képek hozzáadása a dokumentumhoz (csak SUB-os képek)
        for wo, images in wo_grouped_images.items():
            images_with_sub = [image for image in images if "SUB" in image]
            if not images_with_sub:
                continue

            self.document.add_page_break()
            self.document.add_paragraph(f"WO: {wo}", style="Heading 1")

            table = self.document.add_table(rows=0, cols=8)
            row_cells = table.add_row().cells
            cell_index = 0

            for image in images_with_sub:
                img_path = os.path.join(self.temp_recursive_dir, image)
                paragraph = row_cells[cell_index].paragraphs[0]
                run = paragraph.add_run()
                run.add_picture(img_path, width=Inches(1.0))

                sub_values = re.findall(r"SUB\d+ ([\w-]+)", image)
                image_name = f"PN: {sub_values[-1]}" if sub_values else "PN: N/A"
                row_cells[cell_index].add_paragraph(image_name)

                cell_index += 1
                if cell_index >= 8:
                    row_cells = table.add_row().cells
                    cell_index = 0

    def file_processing(self, input_file_address, wo_list, parts, sub_list):
        self.image_tracking = []
        file_root = input_file_address.replace(".txt", "")

        # DOCX stílusok
        style = self.document.styles["Normal"]
        paragraph_format = self.document.styles["Normal"].paragraph_format
        font = style.font
        font.name = "Courier New"
        font.size = Pt(9.5)
        paragraph_format.space_after = Pt(0)

        # Oldalbeállítás
        section = self.document.sections[0]
        section.page_height = Inches(8.27)
        section.page_width = Inches(11.69)
        section.left_margin = Inches(0.3)
        section.right_margin = Inches(0.3)
        section.top_margin = Inches(0.3)
        section.bottom_margin = Inches(0.3)
        section.orientation = WD_ORIENT.LANDSCAPE

        file_path = os.path.abspath(file_root + ".docx")

        with open(input_file_address, "r", encoding="latin-1", errors="ignore") as openfile:
            buffer_string = ""
            for line in openfile:
                line = line.replace("\f", " ")
                if "WO.PRINT" in line:
                    if buffer_string == "":
                        self.document.add_paragraph(buffer_string)
                        buffer_string = line
                    elif buffer_string[-1] == "\n":
                        buffer_string = buffer_string.rstrip(buffer_string[-1])
                        self.document.add_paragraph(buffer_string)
                        self.document.add_page_break()
                        buffer_string = line
                    else:
                        self.document.add_paragraph(buffer_string)
                        self.document.add_page_break()
                        buffer_string = line

                elif "WORK ORDER NBR:" in line:
                    buffer_string += line
                    temp_string = " ".join(line.split()).split(" ")
                    wo_number = temp_string[3]

                    sub_status, part_number, _ = self.is_simple(wo_number, wo_list, sub_list, parts)
                    self.wo_pn_list.append((wo_number, part_number))

                    if part_number:
                        picture_location = os.path.join(self.temp_dir, f"{wo_number}_{part_number}.jpg")
                        if "PAGE" not in buffer_string:
                            if os.path.exists(picture_location):
                                last_paragraph = self.document.paragraphs[-1] if self.document.paragraphs else self.document.add_paragraph("")
                                run = last_paragraph.add_run()
                                run.add_picture(picture_location, width=Inches(1.0))
                            else:
                                # ide kellett volna QR – ne némán maradjon ki
                                logging.warning(
                                    "QR kep hianyzik, kimarad a dokumentumbol: %s (WO=%s, PN=%s)",
                                    picture_location, wo_number, part_number,
                                )

                    if self.document.paragraphs:
                        self.document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.RIGHT

                elif "010  WC#" in line or "010 WC#" in line:
                    self.document.add_paragraph(buffer_string)
                    self.document.add_page_break()
                    buffer_string = line
                else:
                    buffer_string += line

            if buffer_string:
                self.document.add_paragraph(buffer_string)

        # cleanup + képek oldalakra
        self.clean_up()
        self.document.save(file_path)
        return file_path

    # -------------------- Cleanup --------------------

    def export_filtered_wo_and_pn_to_file(self):
        try:
            seen, filtered = set(), []
            for wo, pn in self.wo_pn_list:
                key = f"{wo}|{pn}"
                if key not in seen:
                    seen.add(key)
                    filtered.append((wo, pn))
            self.query_filtered_parts(filtered)
        except Exception as e:
            logging.error("Export WO/PN failed: %s", e)

    def clean_up(self):
        try:
            self.processed_pns.clear()
            self.final_results.clear()

            if self.wo_pn_list:
                self.export_filtered_wo_and_pn_to_file()

            self.add_qr_images_to_document()

            # csak a SAJÁT temp könyvtárainkat töröljük, más futásét soha
            if os.path.isdir(self.temp_dir):
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            if os.path.isdir(self.temp_recursive_dir):
                shutil.rmtree(self.temp_recursive_dir, ignore_errors=True)
        except Exception as e:
            logging.error("Cleanup error: %s", e)

    # -------------------- Master/Sub lookup + SUB feldolgozás --------------------

    def master_lookup_mains(self, parts):
        """
        Egyetlen IN(...) lekérdezés az elemenkénti (kapcsolat+query) ciklus
        helyett – 100 tételnél ez 100 kapcsolatnyitást spórol meg.
        A visszaadott listák sorrendje ÉS hossza a `parts` listát követi:
        a t_dump-ban nem szereplő PN helyére None kerül. Ezt a hívók
        (SUB_Processing, is_simple) index szerint párosítják a wo_list /
        master_pn_list elemeivel, ezért a hiányzó sorokat kihagyni nem
        szabad – attól minden további PN egy hellyel elcsúszna, és rossz
        WO-hoz tartozó QR kód készülne.
        """
        sub_list = []
        sub_qty_list = []
        if not parts:
            return sub_list, sub_qty_list

        placeholders = ",".join(["%s"] * len(parts))
        conn = get_pooled_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                SELECT `PART.NBR`, {', '.join(self._TDUMP_COLS)}
                FROM t_dump
                WHERE `PART.NBR` IN ({placeholders})
                """,
                tuple(parts),
            )
            found = {}
            for row in cursor.fetchall():
                found[row[0]] = (row[1], row[2])  # SUBS, SUBS QTYS
                self._tdump_cache[row[0]] = tuple(row[1:])
            # a nem talált PN-eket is cache-eljük, hogy később se menjünk értük DB-be
            for pn_target in parts:
                if pn_target not in found:
                    self._tdump_cache[pn_target] = None
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

        for pn_target in parts:
            hit = found.get(pn_target)
            if hit:
                sub_list.append(hit[0])
                sub_qty_list.append(hit[1])
            else:
                # nincs t_dump sor: helykitöltő, hogy az indexelés ne csússzon el
                sub_list.append(None)
                sub_qty_list.append(None)
        return sub_list, sub_qty_list

    def master_lookup_subs(self, subs, subs_qty):
        sub_list = []
        st_list = []
        grp_list = []
        cell_list = []
        ecn_list = []
        rev_list = []
        qty_list = []

        wo_qty = self._to_int_or_1(self.current_qty_value)

        if subs and "|" in subs:
            split_sub = subs.split("|")
            split_qty = subs_qty.split("|") if subs_qty else []
            if len(split_qty) != len(split_sub):
                # a t_dump-ban a SUBS és a `SUBS QTYS` mező nem mindig egyforma
                # hosszú (pl. "A|B|C" mellé csak "2"); ilyenkor a hiányzó
                # darabszám 1, a fölösleg pedig eldobódik – korábban ez
                # IndexError-ral megállította az egész feldolgozást
                logging.warning(
                    "t_dump SUBS/SUBS QTYS eltero hosszusag: %d vs %d (SUBS=%r, QTYS=%r)",
                    len(split_sub), len(split_qty), subs, subs_qty,
                )
            for sub_index, x in enumerate(split_sub):
                result = self._tdump_row(x)
                if result:
                    # PROD, SUBS, GROUP, CELL, PROD.REV, CURRENT ECN
                    PROD, SUBS, GRP, CELL, REV, ECN = (
                        result[7], result[0], result[2], result[4], result[3], result[5]
                    )
                    ST = None
                    st_list.append(ST)
                    sub_list.append(SUBS)
                    grp_list.append(GRP)
                    cell_list.append(CELL)
                    rev_list.append(REV)
                    ecn_list.append(ECN)
                    raw_qty = split_qty[sub_index] if sub_index < len(split_qty) else "1"
                    sub_q = self._to_int_or_1(raw_qty)
                    qty_list.append(sub_q * wo_qty)

        elif subs:  # egyetlen SUB PN
            result = self._tdump_row(subs)
            if result:
                # PROD, SUBS, GROUP, CELL, PROD.REV, CURRENT ECN
                PROD, SUBS, GRP, CELL, REV, ECN = (
                    result[7], result[0], result[2], result[4], result[3], result[5]
                )
                ST = None
                st_list.append(ST)
                sub_list.append(SUBS)
                grp_list.append(GRP)
                cell_list.append(CELL)
                rev_list.append(REV)
                ecn_list.append(ECN)
                sub_q = self._to_int_or_1(subs_qty)
                qty_list.append(sub_q * wo_qty)
        else:
            return [], [], [], [], [], [], []

        return sub_list, st_list, grp_list, cell_list, ecn_list, rev_list, qty_list

    def SUB_Processing(self, WO_S, parts, wo_list, filename, master_pn_list):
        transform_var = self.master_lookup_mains(parts)
        sub_list = transform_var[0]
        sub_qty_list = transform_var[1]
        main_parts = parts

        subik_list = []
        st_list = []
        grp_list = []
        cell_list = []
        ecn_list = []
        rev_list = []
        amount_list = []

        for idx, (sub, qty) in enumerate(zip(sub_list, sub_qty_list)):
            WO_current = wo_list[idx]
            self.current_qty_value = self._to_int_or_1(self.wo_qty_mapping.get(WO_current, 1))
            sub = "" if sub is None else str(sub)
            qty = "1" if qty is None else str(qty)
            processed_subs = self.master_lookup_subs(sub, qty)
            subik_list.append(processed_subs[0])
            st_list.append(processed_subs[1])
            grp_list.append(processed_subs[2])
            cell_list.append(processed_subs[3])
            ecn_list.append(processed_subs[4])
            rev_list.append(processed_subs[5])
            amount_list.append(processed_subs[6])

        WO_S_SUBS: List[str] = []
        for index, sub in enumerate(sub_list):
            WO_current = wo_list[index]
            if not sub:
                continue

            QTY_current = str(amount_list[index]) or "N/A"
            DD_current = "##"
            GRP_current = str(grp_list[index]) or "N/A"
            REV_current = str(rev_list[index]) or "N/A"
            CELL_current = str(cell_list[index]) or "N/A"
            ECN_current = str(ecn_list[index]) or "N/A"
            ST_current = str(st_list[index]) or "N/A"
            MLT_current = "M0"
            master_pn = master_pn_list[index]

            if len(sub) > 1:
                sub_parts = sub.split("|")
                qty_parts = QTY_current.strip("[]").split(",")
                rev_parts = REV_current.strip("[]").split(",")
                cell_parts = CELL_current.strip("[]").split(",")
                ecn_parts = ECN_current.strip("[]").split(",")
                grp_parts = GRP_current.strip("[]").split(",")
                st_parts = ST_current.strip("[]").split(",")

                if not (
                    len(sub_parts)
                    == len(qty_parts)
                    == len(rev_parts)
                    == len(cell_parts)
                    == len(ecn_parts)
                    == len(grp_parts)
                    == len(st_parts)
                ):
                    continue

                for sub_part_index, sub_part in enumerate(sub_parts):
                    QTY_Target = qty_parts[sub_part_index].strip(" []'") or "N/A"
                    REV_Target = rev_parts[sub_part_index].strip(" []'") or "N/A"
                    CELL_Target = cell_parts[sub_part_index].strip(" []'") or "N/A"
                    ECN_Target = ecn_parts[sub_part_index].strip(" []'") or "N/A"
                    GRP_Target = grp_parts[sub_part_index].strip(" []'") or "N/A"
                    ST_Target = st_parts[sub_part_index].strip(" []'") or "N/A"

                    if ST_Target != "N/A" and QTY_Target != "N/A":
                        try:
                            STT_Target = str(float(ST_Target) * float(QTY_Target))
                        except Exception:
                            STT_Target = "N/A"
                    else:
                        STT_Target = "N/A"

                    Target_string = (
                        f"WO-{WO_current}|GRP-{GRP_Target}|PN-{sub_part}|MASTER_PN-{master_pn}"
                        f"|DD-{DD_current}|REV-{REV_Target}|CLL-{CELL_Target}|MLT-{MLT_current}-{sub_part_index}"
                        f"|QTY-{QTY_Target}|ECN-{ECN_Target}|ST-{ST_Target}|STT-{STT_Target}"
                    )
                    WO_S_SUBS.append(Target_string)
            else:
                QTY_Target = QTY_current if QTY_current != "N/A" else str(self._to_int_or_1(self.current_qty_value))
                try:
                    STT = str(float(ST_current) * float(QTY_Target))
                except Exception:
                    STT = "N/A"

                Target_string = (
                    f"WO-{WO_current}|GRP-{GRP_current}|PN-{sub}|MASTER_PN-{master_pn}|DD-{DD_current}"
                    f"|REV-{REV_current}|CLL-{CELL_current}|MLT-{MLT_current}-{index}|QTY-{QTY_Target}"
                    f"|ECN-{ECN_current}|ST-{ST_current}|STT-{STT}"
                )
                WO_S_SUBS.append(Target_string)

        self.create_QR(WO_S, "sub", "")
        self.create_QR(WO_S_SUBS, "sub", "")
        return self.file_processing(filename, wo_list, main_parts, sub_list)

    # -------------------- Publikus belépési pont Flaskhez --------------------

    # A WO.PRINT riport akkor is sima szöveg, ha .xls néven érkezik, ezért a
    # kiterjesztés helyett a fájl első bájtjaiból döntjük el, mi az valójában.
    _OOXML_MAGIC = b"PK\x03\x04"          # .xlsx / .docx / bármilyen ZIP alapú OOXML
    _OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # régi bináris Excel 97-2003

    def _tabtext_to_txt(self, input_path: str) -> str:
        """Szöveges (tab-elválasztású vagy fix szélességű) input átírása .txt-be."""
        txt_path = os.path.splitext(input_path)[0] + ".txt"
        if os.path.abspath(txt_path) == os.path.abspath(input_path):
            return input_path
        with open(
            input_path, mode="r", newline="", encoding="latin-1", errors="ignore"
        ) as f_in, open(
            txt_path, mode="w", newline="", encoding="latin-1", errors="ignore"
        ) as f_out:
            # QUOTE_NONE: a riportban előfordulnak idézőjelek (pl. TYPE "R" IF
            # REWORK), ezeket a csv modul alapból mezőhatárolónak nézhetné
            reader = csv.reader(f_in, delimiter="\t", quoting=csv.QUOTE_NONE)
            for row in reader:
                f_out.write("\t".join(row) + "\n")
        return txt_path

    def _xlsx_to_txt(self, input_path: str) -> str:
        """Valódi .xlsx munkafüzet első munkalapjának kiírása tab-elválasztva."""
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ValueError(
                "A feltöltött fájl valódi Excel munkafüzet (.xlsx), ennek olvasásához "
                "az openpyxl csomag szükséges a szerveren (pip install openpyxl). "
                "Addig mentsd a WO.PRINT riportot szövegként (.txt) és úgy töltsd fel."
            ) from exc

        txt_path = os.path.splitext(input_path)[0] + ".txt"
        wb = load_workbook(input_path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            with open(txt_path, mode="w", newline="", encoding="latin-1", errors="ignore") as f_out:
                for row in ws.iter_rows(values_only=True):
                    f_out.write("\t".join("" if c is None else str(c) for c in row) + "\n")
        finally:
            try:
                wb.close()
            except Exception:
                pass
        return txt_path

    def _prepare_input(self, input_path: str) -> str:
        """A feltöltött fájlból előállítja a feldolgozható .txt útvonalat."""
        with open(input_path, "rb") as fh:
            head = fh.read(8)

        if head.startswith(self._OOXML_MAGIC):
            return self._xlsx_to_txt(input_path)

        if head.startswith(self._OLE2_MAGIC):
            raise ValueError(
                "A feltöltött fájl régi, bináris Excel munkafüzet (.xls, Excel 97-2003), "
                "amit a WO Generator nem tud olvasni. Nyisd meg Excelben és mentsd el "
                ".xlsx vagy .txt formátumban, majd töltsd fel újra."
            )

        # Sima szöveg. A WO.PRINT riport tipikusan ilyen, akkor is, ha .xls a neve.
        if input_path.lower().endswith((".xls", ".xlsx")):
            return self._tabtext_to_txt(input_path)
        return input_path

    def process_file(self, input_path: str, *, enable_word_com: bool = False, preview: bool = False) -> str:
        """
        Végigviszi a teljes folyamatot az adott TXT (vagy .xls/.xlsx) fájlra.
        Visszaadja a létrejött .docx abszolút útvonalát.
        """
        self.set_preview(preview)

        path = self._prepare_input(input_path)

        WO_S, parts, wo_list, master_pn_list = self.data_extract(path)
        if not WO_S:
            # korábban ilyenkor némán egy üres DOCX készült
            raise ValueError(
                "A fájlban nem található feldolgozható WO.PRINT adat "
                "(nincs benne 'WORK ORDER NBR:' / 'PART NUMBER:' blokk). "
                "Ellenőrizd, hogy a WO.PRINT riportot töltötted-e fel."
            )

        out_docx = self.SUB_Processing(WO_S, parts, wo_list, path, master_pn_list)
        self.flush_pending_rows()
        return out_docx
