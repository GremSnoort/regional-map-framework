"""Read saved Rosstat tables with explicit worksheet, row and column provenance."""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
URL_2025 = "https://rosstat.gov.ru/storage/mediabank/%D0%A1hisl_MO_01-01-2025.xlsx"
URL_CENSUS = "https://rosstat.gov.ru/storage/mediabank/Tom1_tab-5_VPN-2020.xlsx"


def norm(value):
    return re.sub(r"[^а-яa-z0-9]+", "", value.casefold().replace("ё", "е"))


def locality(value):
    # кп is an administrative category (курортный поселок), not part of the name.
    return re.sub(r"\s+кп$", "", value.strip(), flags=re.I)


def district_key(value):
    value = value.casefold().replace("ё", "е")
    return norm(re.sub(r"\b(?:городской|муниципальный|округ|район|город|зато|г)\b", "", value))


def worksheet(path: Path, number: int):
    with zipfile.ZipFile(path) as archive:
        strings = ["".join(n.itertext()) for n in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall("m:si", NS)]
        root = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
        for row in root.findall(".//m:sheetData/m:row", NS):
            values = {}
            for cell in row:
                value = cell.find("m:v", NS)
                text = "" if value is None else value.text or ""
                if cell.get("t") == "s":
                    text = strings[int(text)]
                elif cell.get("t") == "inlineStr":
                    text = "".join(n.text or "" for n in cell.findall(".//m:t", NS))
                values[re.sub(r"\d", "", cell.get("r"))] = text.strip()
            yield int(row.get("r")), values


def controls(source_root: Path):
    municipalities, current, census = {}, defaultdict(list), defaultdict(list)
    active = False
    context = ""
    for row, values in worksheet(source_root / "rosstat_municipal_population_2025.xlsx", 2):
        name, code = values.get("B", ""), values.get("A", "").replace(" ", "")
        if name == "Приморский край":
            active = True
            continue
        if not active:
            continue
        if len(code) == 10 and not code.startswith("05"):
            break
        if not values.get("C", "").isdigit():
            continue
        value = int(values["C"])
        record = {"population": value, "population_as_of": "2025-01-01", "population_quality": "official_current",
                  "population_source": "Росстат, численность по муниципальным образованиям на 01.01.2025",
                  "population_source_url": URL_2025, "source_file": "rosstat_municipal_population_2025.xlsx",
                  "source_sheet": "Численность_по_МО", "source_row": row, "source_cell": f"C{row}",
                  "source_name": name, "source_oktmo": code}
        if len(code) == 10 and any(t in name.casefold() for t in ("муниципальный округ", "муниципальный район", "городской округ")):
            municipalities[name] = record
            context = name
        match = re.match(r"^(?:г|пгт)\s+(.+)$", name, re.I)
        if match and len(code) >= 11:
            current[norm(locality(match[1]))].append(record | {"source_district": context})
    if len(municipalities) != 34 or sum(v["population"] for v in municipalities.values()) != 1_799_659:
        raise ValueError("Saved 2025 municipal table does not reconcile")
    active, context = False, ""
    for row, values in worksheet(source_root / "rosstat_vpn2020_table5.xlsx", 1):
        title = values.get("A", "")
        if title == "Приморский край":
            active = True
            continue
        if title == "Хабаровский край" and active:
            break
        if not active:
            continue
        heading = re.match(r"^(.+?(?:городской округ|муниципальный район|муниципальный округ)|Городской округ[^-]+)", title, re.I)
        if heading:
            context = heading[1].strip()
        match = re.search(r"(?:^|\s-\s)(?:городское население\s*-\s*)?(?:г\.|пгт\.?|село|поселок|посёлок|деревня)\s+([^–—]+)$", title, re.I)
        if not match or not values.get("B", "").isdigit():
            continue
        census[norm(locality(match[1]))].append({
            "population": int(values["B"]), "population_as_of": "2021-10-01", "population_quality": "official_census_exact",
            "population_source": "Росстат, ВПН-2020, том 1, таблица 5; дата переписи 01.10.2021",
            "population_source_url": URL_CENSUS, "source_file": "rosstat_vpn2020_table5.xlsx",
            "source_sheet": "таб. 5", "source_row": row, "source_cell": f"B{row}",
            "source_name": title, "source_district": context})
    return municipalities, current, census


def official_match(properties, municipal_properties, current, census):
    name, district = norm(properties["name"]), properties.get("district", "")
    matching = [p for p in municipal_properties if p["municipality_name"] == district]
    aliases = {district_key(district)}
    for p in matching:
        aliases.update(district_key(n.strip()) for n in p["official_name"].split(" + "))
    # The census names of these two city districts omit "город".
    aliases.update({"большойкамень"} if "большойкамень" in district_key(district) else set())
    aliases.update({"фокино"} if "фокино" in district_key(district) else set())
    for table in (current, census):
        rows = [r for r in table.get(name, []) if district_key(r["source_district"]) in aliases]
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise ValueError(f"Ambiguous official source for {properties['name']} / {district}")
    return None
