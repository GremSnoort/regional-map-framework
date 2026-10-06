#!/usr/bin/env python3
"""Collect, normalize, deduplicate, review and publish regional contacts."""
from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import manage

USER_AGENT = "RegionalMapFrameworkContactCollector/1.0 (+public-business-contacts)"
CONTACT_KINDS = {"agency", "realtor", "broker", "developer", "property_manager", "other"}
LINK_TYPES = {"telegram", "whatsapp", "vk", "ok", "youtube", "cian", "avito", "yandex", "2gis", "other"}
PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)[\s()\-]*(?:\d[\s()\-]*){10}(?!\d)")
EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def compact(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strings(value: object) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [item for item in (compact(entry) for entry in items) if item]


def unique(items: list, key=lambda item: item) -> list:
    result, seen = [], set()
    for item in items:
        marker = key(item)
        if marker not in seen:
            seen.add(marker); result.append(item)
    return result


def normalize_phone(value: object) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 7 <= len(digits) <= 15:
        return None
    return "+" + digits


def normalize_email(value: object) -> str | None:
    item = compact(value).lower()
    return item if EMAIL_RE.fullmatch(item) else None


def normalize_url(value: object) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(compact(value))
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host if port is None or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def link_type(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in {"t.me", "telegram.me"}: return "telegram"
    if "wa.me" in host or "whatsapp" in host: return "whatsapp"
    if host.endswith("vk.com"): return "vk"
    if host.endswith("ok.ru"): return "ok"
    if "youtube" in host or host == "youtu.be": return "youtube"
    if host.endswith("cian.ru"): return "cian"
    if host.endswith("avito.ru"): return "avito"
    if host.endswith("yandex.ru"): return "yandex"
    if host.endswith("2gis.ru"): return "2gis"
    return "other"


def normalized_name(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"\b(?:ооо|ао|ип|агентство недвижимости|агентство|недвижимость)\b", " ", value)
    return re.sub(r"[^a-zа-я0-9]+", "", value)


def normalize_links(value: object) -> list[dict]:
    rows = value if isinstance(value, list) else []
    result = []
    for row in rows:
        if isinstance(row, str):
            url, label, kind = normalize_url(row), "", ""
        elif isinstance(row, dict):
            url, label, kind = normalize_url(row.get("url")), compact(row.get("label")), compact(row.get("type")).lower()
        else:
            continue
        if not url:
            continue
        if kind not in LINK_TYPES:
            kind = link_type(url)
        result.append({"type": kind, "label": label or kind, "url": url})
    return unique(result, lambda item: item["url"])


def normalize_contact(raw: dict, source: dict, retrieved_at: str, region_id: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("contact must be an object")
    name = compact(raw.get("name") or raw.get("display_name"))
    if not name:
        raise ValueError("contact name is required")
    kind = compact(raw.get("kind") or raw.get("entity_type") or "other").lower()
    if kind not in CONTACT_KINDS:
        kind = "other"
    phones = unique([item for item in (normalize_phone(value) for value in strings(raw.get("phones") or raw.get("phone"))) if item])
    emails = unique([item for item in (normalize_email(value) for value in strings(raw.get("emails") or raw.get("email"))) if item])
    websites = unique([item for item in (normalize_url(value) for value in strings(raw.get("websites") or raw.get("website"))) if item])
    links = normalize_links(raw.get("links") or raw.get("profiles") or raw.get("messengers"))
    coverage = unique(strings(raw.get("coverage") or raw.get("areas") or [region_id]), str.casefold)
    specializations = unique(strings(raw.get("specializations")), str.casefold)
    addresses = unique(strings(raw.get("addresses") or raw.get("address")), str.casefold)
    if not any((phones, emails, websites, links, addresses)):
        raise ValueError(f"{name}: at least one public contact channel or address is required")
    source_url = normalize_url(source.get("url") or (source.get("urls") or [None])[0])
    reference = {"source_id": source["source_id"], "retrieved_at": retrieved_at}
    if source_url:
        reference["url"] = source_url
    result = {
        "contact_id": compact(raw.get("contact_id")),
        "name": name,
        "kind": kind,
        "coverage": coverage,
        "specializations": specializations,
        "phones": phones,
        "emails": emails,
        "websites": websites,
        "addresses": addresses,
        "links": links,
        "sources": [reference],
        "review_status": "needs_review",
    }
    return result


def evidence_keys(contact: dict) -> set[str]:
    result = {f"p:{value}" for value in contact["phones"]} | {f"e:{value}" for value in contact["emails"]}
    for value in contact["websites"]:
        host = urllib.parse.urlsplit(value).hostname
        if host: result.add(f"d:{host.removeprefix('www.')}")
    name = normalized_name(contact["name"])
    if name:
        result.add(f"n:{name}|{','.join(sorted(value.casefold() for value in contact['coverage']))}")
    return result


def merge_contacts(items: list[dict]) -> list[dict]:
    parents = list(range(len(items)))
    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]; index = parents[index]
        return index
    def union(left, right):
        left, right = find(left), find(right)
        if left != right: parents[right] = left
    owners: dict[str, int] = {}
    for index, item in enumerate(items):
        for key in evidence_keys(item):
            if key in owners: union(index, owners[key])
            else: owners[key] = index
    groups: dict[int, list[dict]] = {}
    for index, item in enumerate(items): groups.setdefault(find(index), []).append(item)
    output = []
    for rows in groups.values():
        rows.sort(key=lambda row: (len(row["phones"])+len(row["emails"])+len(row["websites"])+len(row["links"]), len(row["name"])), reverse=True)
        result = {key: rows[0][key] for key in ("name", "kind")}
        for field in ("coverage", "specializations", "phones", "emails", "websites", "addresses"):
            result[field] = unique([value for row in rows for value in row[field]], lambda value: value.casefold())
        result["links"] = unique([value for row in rows for value in row["links"]], lambda value: value["url"])
        result["sources"] = unique([value for row in rows for value in row["sources"]], lambda value: (value["source_id"], value.get("url", "")))
        supplied = next((row["contact_id"] for row in rows if row["contact_id"]), "")
        keys = sorted(set().union(*(evidence_keys(row) for row in rows)))
        identity = supplied or (keys[0] if keys else normalized_name(result["name"]))
        result["contact_id"] = supplied or "contact_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        result["review_status"] = "needs_review"
        output.append(result)
    return sorted(output, key=lambda item: (item["name"].casefold(), item["contact_id"]))


class ContactHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.text = []; self.hrefs = []
    def handle_data(self, data):
        self.text.append(data)
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href: self.hrefs.append(html.unescape(href))


def ensure_public_hostname(host: str) -> None:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise ValueError(f"cannot resolve source host {host}: {error}") from error
    if not addresses:
        raise ValueError(f"source host has no addresses: {host}")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if not address.is_global:
            raise ValueError(f"source host resolves to a non-public address: {host}")


def checked_url(value: str, allowed_hosts: list[str]) -> str:
    url = normalize_url(value)
    if not url or urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("remote contact sources require HTTPS URLs")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    allowed = {item.lower().rstrip(".") for item in allowed_hosts}
    if host not in allowed:
        raise ValueError(f"source host is not allowlisted: {host}")
    ensure_public_hostname(host)
    return url


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: list[str]):
        super().__init__(); self.allowed_hosts = allowed_hosts
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        checked_url(new_url, self.allowed_hosts)
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


def open_remote(url: str, source: dict, limit: int = 2_000_000) -> tuple[bytes, str]:
    allowed = source["allowed_hosts"]
    url = checked_url(url, allowed)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"})
    opener = urllib.request.build_opener(CheckedRedirect(allowed))
    with opener.open(request, timeout=20) as response:
        final = checked_url(response.geturl(), allowed)
        length = response.headers.get("Content-Length")
        if length and int(length) > limit: raise ValueError("remote source is too large")
        body = response.read(limit + 1)
        if len(body) > limit: raise ValueError("remote source is too large")
        return body, final


def robots_allowed(url: str, source: dict) -> bool:
    parsed = urllib.parse.urlsplit(url)
    robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
    parser = urllib.robotparser.RobotFileParser(); parser.set_url(robots_url)
    try:
        body, _ = open_remote(robots_url, source, limit=256_000)
        parser.parse(body.decode("utf-8", "replace").splitlines())
    except urllib.error.HTTPError as error:
        if error.code == 404: return True
        return False
    except (OSError, ValueError):
        return False
    return parser.can_fetch(USER_AGENT, url)


def local_json(root: Path, source: dict) -> list[dict]:
    relative = Path(source["path"])
    path = (root / relative).resolve()
    contacts_root = (root / "contacts").resolve()
    if contacts_root != path.parent and contacts_root not in path.parents:
        raise ValueError("local contact source escapes contacts/")
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("contacts") if isinstance(value, dict) else value
    if not isinstance(rows, list): raise ValueError("local JSON source must contain a contacts array")
    return rows


def remote_json(source: dict) -> list[dict]:
    body, _ = open_remote(source["url"], source)
    value = json.loads(body.decode("utf-8"))
    rows = value.get(source.get("contacts_key", "contacts")) if isinstance(value, dict) else value
    if not isinstance(rows, list): raise ValueError("remote JSON source must contain a contacts array")
    return rows


def website_contact(source: dict) -> dict:
    phones, emails, websites, links, addresses = [], [], [], [], strings(source.get("addresses"))
    for index, requested in enumerate(source["urls"]):
        url = checked_url(requested, source["allowed_hosts"])
        if source.get("respect_robots_txt", True) and not robots_allowed(url, source):
            raise ValueError(f"robots.txt does not allow collection: {url}")
        body, final = open_remote(url, source)
        parser = ContactHTMLParser(); parser.feed(body.decode("utf-8", "replace"))
        text = compact(" ".join(parser.text))
        phones.extend(match.group(0) for match in PHONE_RE.finditer(text))
        emails.extend(match.group(0) for match in EMAIL_RE.finditer(text))
        websites.append(final)
        for href in parser.hrefs:
            absolute = urllib.parse.urljoin(final, href)
            parsed = urllib.parse.urlsplit(absolute)
            if parsed.scheme == "tel": phones.append(parsed.path)
            elif parsed.scheme == "mailto": emails.append(parsed.path.split("?", 1)[0])
            else:
                normalized = normalize_url(absolute)
                if normalized and link_type(normalized) != "other": links.append({"url": normalized})
        if index + 1 < len(source["urls"]): time.sleep(source.get("request_delay_seconds", 2))
    return {"name": source["name"], "kind": source.get("kind", "other"), "coverage": source.get("coverage", []), "specializations": source.get("specializations", []), "phones": phones, "emails": emails, "websites": websites, "links": links, "addresses": addresses}


def source_rows(root: Path, source: dict) -> list[dict]:
    kind = source["type"]
    if kind == "local_json": return local_json(root, source)
    if kind == "remote_json": return remote_json(source)
    if kind == "website": return [website_contact(source)]
    raise ValueError(f"unsupported source type: {kind}")


def preserve_published_ids(items: list[dict], previous: dict) -> None:
    owners: dict[str, set[str]] = {}
    for row in previous.get("contacts", []):
        for key in evidence_keys(row): owners.setdefault(key, set()).add(row["contact_id"])
    used = set()
    for row in items:
        matches = set().union(*(owners.get(key, set()) for key in evidence_keys(row)))
        if len(matches) == 1:
            identifier = next(iter(matches))
            if identifier not in used: row["contact_id"] = identifier; used.add(identifier)


def comparable_contact(row: dict) -> dict:
    value = {key: item for key, item in row.items() if key != "review_status"}
    value["sources"] = [{key: item for key, item in source.items() if key != "retrieved_at"} for source in row.get("sources", [])]
    return value


def comparison(previous: dict, candidate: dict) -> dict:
    old = {row["contact_id"]: row for row in previous.get("contacts", [])}
    new = {row["contact_id"]: row for row in candidate.get("contacts", [])}
    return {
        "added": sorted(new.keys() - old.keys()),
        "changed": sorted(key for key in new.keys() & old.keys() if comparable_contact(new[key]) != comparable_contact(old[key])),
        "removed": sorted(old.keys() - new.keys()),
        "unchanged": len([key for key in new.keys() & old.keys() if comparable_contact(new[key]) == comparable_contact(old[key])]),
    }


def validate_catalog(value: dict, region_id: str, allow_candidates: bool = True) -> dict:
    if value.get("schema_version") != 1 or value.get("region_id") != region_id or not isinstance(value.get("contacts"), list):
        raise ValueError("invalid contact catalog identity")
    identifiers = set()
    for row in value["contacts"]:
        required = {"contact_id", "name", "kind", "coverage", "specializations", "phones", "emails", "websites", "addresses", "links", "sources", "review_status"}
        if not isinstance(row, dict) or set(row) != required: raise ValueError("contact catalog row fields are invalid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", row["contact_id"]): raise ValueError("invalid contact_id")
        if row["contact_id"] in identifiers: raise ValueError("duplicate contact_id")
        identifiers.add(row["contact_id"])
        if not compact(row["name"]) or row["kind"] not in CONTACT_KINDS: raise ValueError("invalid contact name or kind")
        for field in ("coverage", "specializations", "phones", "emails", "websites", "addresses", "links", "sources"):
            if not isinstance(row[field], list): raise ValueError(f"contact {row['contact_id']}.{field} must be an array")
        if any(normalize_phone(value) != value for value in row["phones"]): raise ValueError("contact catalog contains a non-normalized phone")
        if any(normalize_email(value) != value for value in row["emails"]): raise ValueError("contact catalog contains an invalid email")
        if any(normalize_url(value) != value for value in row["websites"]): raise ValueError("contact catalog contains an invalid website")
        if any(not isinstance(value, str) or not value for field in ("coverage", "specializations", "addresses") for value in row[field]): raise ValueError("contact catalog contains an invalid text value")
        if any(not isinstance(link, dict) or set(link) != {"type", "label", "url"} or link["type"] not in LINK_TYPES or not compact(link["label"]) or normalize_url(link["url"]) != link["url"] for link in row["links"]): raise ValueError("contact catalog contains an invalid link")
        if any(not isinstance(source, dict) or set(source) not in ({"source_id", "retrieved_at"}, {"source_id", "retrieved_at", "url"}) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source.get("source_id", "")) or not compact(source.get("retrieved_at")) or ("url" in source and normalize_url(source["url"]) != source["url"]) for source in row["sources"]): raise ValueError("contact catalog contains an invalid source reference")
        if row["review_status"] not in ({"needs_review", "published"} if allow_candidates else {"published"}): raise ValueError("invalid review status")
    return value


def paths(root: Path) -> dict[str, Path]:
    base = manage.runtime_dir(root) / "contacts"
    return {name: base / filename for name, filename in {"base": ".", "state": "state.json", "candidates": "candidates.json", "published": "published.json", "lock": ".collection.lock"}.items()}


def collect(region_id: str, reserved_lock: bool = False) -> dict:
    root = manage.region_dir(manage.safe_id(region_id, "region_id")); config = manage.read_json(root / "region.json")
    spec = manage.validate_contacts(root, config); target = paths(root); target["base"].mkdir(parents=True, exist_ok=True)
    if not spec or not spec.get("enabled"): raise ValueError("contact collection is not enabled")
    if reserved_lock:
        if not target["lock"].is_file(): raise ValueError("reserved contact collection lock is missing")
    else:
        try: descriptor = os.open(target["lock"], os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error: raise ValueError("contact collection is already running") from error
        with os.fdopen(descriptor, "w", encoding="ascii") as stream: stream.write(str(os.getpid()))
    started = now(); state = {"schema_version": 1, "region_id": region_id, "status": "running", "started_at": started}
    try:
        target["lock"].write_text(str(os.getpid()), encoding="ascii"); manage.atomic_json(target["state"], state)
        rows, errors, successful = [], [], 0
        for index, source in enumerate(spec["sources"]):
            retrieved = now()
            try:
                raw_rows = source_rows(root, source)
                rows.extend(normalize_contact(row, source, retrieved, region_id) for row in raw_rows); successful += 1
            except Exception as error:
                errors.append({"source_id": source["source_id"], "error": compact(error)[:500]})
            if index + 1 < len(spec["sources"]): time.sleep(spec.get("request_delay_seconds", 2))
        if not successful: raise ValueError("all contact sources failed: " + json.dumps(errors, ensure_ascii=False))
        contacts = merge_contacts(rows); generated = now()
        previous = manage.read_json(target["published"]) if target["published"].is_file() else {"contacts": []}
        preserve_published_ids(contacts, previous)
        catalog = {"schema_version": 1, "region_id": region_id, "generated_at": generated, "contacts": contacts, "source_errors": errors}
        validate_catalog(catalog, region_id); manage.atomic_json(target["candidates"], catalog)
        diff = comparison(previous, catalog)
        state |= {"status": "succeeded", "finished_at": generated, "last_success_at": generated, "candidate_count": len(contacts), "source_errors": errors, "comparison": diff}
        manage.atomic_json(target["state"], state); return state
    except BaseException as error:
        state |= {"status": "failed", "finished_at": now(), "error": compact(error)[:1000]}; manage.atomic_json(target["state"], state); raise
    finally:
        target["lock"].unlink(missing_ok=True)


def publish(region_id: str, reviewer: str) -> dict:
    root = manage.region_dir(manage.safe_id(region_id, "region_id")); config = manage.read_json(root / "region.json")
    spec = manage.validate_contacts(root, config); target = paths(root)
    if not spec or not spec.get("enabled"): raise ValueError("contact collection is not enabled")
    if target["lock"].exists(): raise ValueError("contact collection is still running")
    if not target["candidates"].is_file(): raise ValueError("candidate catalog is missing")
    catalog = validate_catalog(manage.read_json(target["candidates"]), region_id)
    published_at = now()
    for row in catalog["contacts"]: row["review_status"] = "published"
    catalog |= {"published_at": published_at, "published_by": reviewer}
    validate_catalog(catalog, region_id, allow_candidates=False); manage.atomic_json(target["published"], catalog)
    state = manage.read_json(target["state"]) if target["state"].is_file() else {"schema_version": 1, "region_id": region_id}
    state |= {"published_at": published_at, "published_by": reviewer, "published_count": len(catalog["contacts"]), "comparison": {"added": [], "changed": [], "removed": [], "unchanged": len(catalog["contacts"])} }
    manage.atomic_json(target["state"], state); return catalog


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--region", required=True); parser.add_argument("--reserved-lock", action="store_true"); parser.add_argument("--publish", action="store_true"); parser.add_argument("--reviewer", default="console-admin"); args = parser.parse_args()
    result = publish(args.region, args.reviewer) if args.publish else collect(args.region, args.reserved_lock)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
