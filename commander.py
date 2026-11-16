"""Convert authorized exports according to an explicitly supplied field map."""
import csv
from io import StringIO
import re
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from xml.etree import ElementTree

from cameras import NoRedirect

LIMIT = 5 * 1024 * 1024
REQUIRED_TLOG_COLUMNS = {"transaction_id", "line_id", "occurred_at", "kind", "quantity", "amount"}


def tlog_csv(payload, mapping, fields):
    if len(payload) > LIMIT:
        raise ValueError("The XML export exceeds 5 MiB.")
    text = payload.decode("utf-8-sig")
    if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are unsupported.")
    try:
        document = ElementTree.fromstring(text)
        namespaces = mapping.get("namespaces", {})
        configured = set(mapping["fields"])
        if configured - set(fields):
            raise ValueError("The T-Log map contains unknown output columns.")
        if REQUIRED_TLOG_COLUMNS - configured:
            raise ValueError("The T-Log map is missing one or more required output columns.")
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        count = 0
        for event in document.findall(mapping["events"], namespaces):
            for item in event.findall(mapping["lines"], namespaces):
                row = {}
                for name, spec in mapping["fields"].items():
                    scope = {"event": event, "line": item}[spec["scope"]]
                    value = scope.findtext(spec["path"], namespaces=namespaces)
                    if value is None:
                        raise ValueError(f"A mapped field is missing: {name}.")
                    value = value.strip()
                    if "values" in spec:
                        value = spec["values"][value]
                    row[name] = value
                writer.writerow(row)
                count += 1
                if output.tell() > LIMIT:
                    raise ValueError("The converted report exceeds 5 MiB.")
        if not count:
            raise ValueError("No transaction lines matched the XML map.")
        return output.getvalue()
    except (ElementTree.ParseError, KeyError, SyntaxError, TypeError, AttributeError):
        raise ValueError("The T-Log XML or field map is invalid.") from None


def import_plu(db, payload, mapping, required):
    if len(payload) > LIMIT:
        raise ValueError("The PLU export exceeds 5 MiB.")
    rows = csv.DictReader(StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
    headers = rows.fieldnames or []
    if len(headers) != len(set(headers)):
        raise ValueError("The PLU export contains duplicate headers.")
    try:
        sku, name, barcode = (mapping[key] for key in ("sku", "name", "barcode"))
        if len({sku, name, barcode}) != 3 or not {sku, name, barcode}.issubset(headers):
            raise ValueError("The PLU export does not match the configured columns.")
    except (KeyError, TypeError):
        raise ValueError("The PLU column map is invalid.") from None
    seen = set()
    for row in rows:
        if None in row or None in row.values():
            raise ValueError("A PLU row does not match the configured columns.")
        code = required(row[sku])
        if code in seen:
            raise ValueError("The PLU export repeats a product code.")
        seen.add(code)
        db.execute("""INSERT INTO products VALUES (?,?,?,0)
                    ON CONFLICT(sku) DO UPDATE SET name=excluded.name, barcode=excluded.barcode""",
                   (code, required(row[name]), required(row[barcode]) if row[barcode].strip() else None))
    if not seen:
        raise ValueError("No products found in the PLU export.")
    return len(seen)


def get_partner_xml(endpoint, authorization):
    """Transport for a partner-confirmed HTTPS GET XML export contract."""
    parts = urlsplit(endpoint)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
        or parts.fragment or not authorization or any(not 32 <= ord(char) < 127 for char in authorization)):
        raise ValueError("Configure an authorized HTTPS XML export endpoint.")
    request = Request(endpoint, headers={"Accept": "application/xml", "Authorization": authorization})
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            if response.headers.get_content_type() not in ("application/xml", "text/xml"):
                raise ValueError("The partner response did not advertise XML.")
            payload = response.read(LIMIT + 1)
        if len(payload) > LIMIT:
            raise ValueError("The partner response exceeds 5 MiB.")
        return payload
    except (URLError, TimeoutError, OSError):
        raise ValueError("Could not read the configured partner export.") from None
