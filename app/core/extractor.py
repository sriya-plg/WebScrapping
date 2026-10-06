"""Scrapling-based page extractor for carrier tracking results.

Extracts shipment fields directly from the rendered HTML DOM using the Scrapling library.
Never inspects network responses or carrier API payloads.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from scrapling import Selector

from app.browser.base import DEFAULT_BLOCK_MARKERS
from app.core.resilience import BlockedError, NotFoundError, TransientError
from app.settings import ScrapingConfig

logger = logging.getLogger(__name__)

# Common header names to normalized field names
HEADER_MAP = {
    "pro number": "reference",
    "pro": "reference",
    "tracking number": "reference",
    "status": "status",
    "shipment status": "status",
    "pickup date": "pickup_date",
    "date picked up": "pickup_date",
    "estimated delivery date": "eta",
    "estimated delivery": "eta",
    "est delivery": "eta",
    "date delivered": "delivery_date",
    "delivery date": "delivery_date",
    "actual delivery": "delivery_date",
    "delivery window": "delivery_window",
    "consignee": "consignee",
    "destination": "destination",
    "shipper": "shipper",
    "origin": "origin",
    "pieces": "pieces",
    "weight": "weight",
}


def parse_date_value(val: str | None) -> str | None:
    """Normalize common carrier date strings (e.g. M/D/YY, M/D/YYYY) to YYYY-MM-DD."""
    if not val:
        return None
    cleaned = val.strip()
    if cleaned in ("-", "N/A", "n/a", "None", "--", ""):
        return None
    # If already ISO YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", cleaned):
        return cleaned[:10]
    for fmt in (
        "%m/%d/%y",
        "%m/%d/%Y",
        "%m-%d-%y",
        "%m-%d-%Y",
        "%b %d, %Y",
        "%m/%d/%Y %I:%M %p",
        "%m/%d/%y %I:%M %p",
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return cleaned


class ScraplingExtractor:
    """Extracts carrier shipment data from rendered page HTML using Scrapling."""

    def __init__(self, config: ScrapingConfig | None = None, block_markers: list[str] | None = None):
        self.config = config or ScrapingConfig()
        self.block_markers = block_markers or DEFAULT_BLOCK_MARKERS

    def extract(self, html: str, ref: str) -> dict[str, Any]:
        """Parse HTML with Scrapling and extract shipment information.

        Raises:
            BlockedError: if page indicates access denied / Cloudflare challenge.
            NotFoundError: if page indicates reference was not found.
            TransientError: if page HTML is empty, malformed, or missing expected result containers.
        """
        if not html or not html.strip():
            raise TransientError("empty HTML received from browser session")

        try:
            sel = Selector(html)
        except Exception as exc:
            raise TransientError(f"Scrapling failed to parse page HTML: {exc}") from exc

        # 1. Check for block / challenge markers
        self._check_blocked(sel)

        # 2. Check for explicit "not found" indicators
        self._check_not_found(sel, ref)

        # 3. Locate tracking result container or row
        target_node = self._locate_result(sel, ref)
        if target_node is None:
            raise TransientError(f"tracking result not found in rendered DOM for {ref}")

        # 4. Extract data fields
        data: dict[str, Any] = {}

        # 4a. Automatic table header extraction if target is within a table
        table_data = self._extract_table_row(sel, target_node, ref)
        data.update(table_data)

        # 4b. Explicit selector fields from scraping configuration
        if self.config.fields:
            for field_name, selector_expr in self.config.fields.items():
                val = self._extract_field(target_node, selector_expr)
                if val is not None:
                    data[field_name] = val

        # 4c. Clean values & harmonize field aliases
        self._post_process(data, ref)

        if not data.get("reference") and not data.get("status"):
            raise TransientError(f"extracted data from page content is empty or invalid for {ref}")

        return data

    def _check_blocked(self, sel: Selector) -> None:
        title_text = ""
        try:
            title_node = sel.css("title").first
            if title_node:
                title_text = str(title_node.get_all_text().clean()).lower()
        except Exception:
            pass

        body_text = ""
        try:
            body_node = sel.css("body").first
            if body_node:
                body_text = str(body_node.get_all_text().clean()[:2000]).lower()
        except Exception:
            pass

        full_sample = f"{title_text} {body_text}"
        for marker in self.block_markers:
            if marker.lower() in full_sample:
                raise BlockedError(f"page blocked by security check ('{marker}')")

    def _check_not_found(self, sel: Selector, ref: str) -> None:
        cfg = self.config

        # Check explicit not_found_selector (e.g. "td.no-pro-found")
        if cfg.not_found_selector:
            try:
                els = self._query(sel, cfg.not_found_selector)
                if els:
                    txt = str(els.first.get_all_text().clean())
                    if txt:
                        logger.info("not-found selector matched: %s -> %r", cfg.not_found_selector, txt)
                        raise NotFoundError(ref)
            except NotFoundError:
                raise
            except Exception as exc:
                logger.warning("error checking not_found_selector: %s", exc)

        # Check explicit not_found_text markers
        for marker in cfg.not_found_text:
            try:
                matches = sel.xpath(f"//*[contains(text(), '{marker}')]")
                if matches:
                    raise NotFoundError(ref)
            except NotFoundError:
                raise
            except Exception:
                pass

        # Built-in carrier not found checks
        try:
            nf_els = sel.css(".no-pro-found, .not-found, .alert-danger, [class*='no-result']")
            for nf in nf_els:
                txt = str(nf.get_all_text().clean()).lower()
                if any(w in txt for w in ["not available", "not found", "no shipment", "invalid"]):
                    raise NotFoundError(ref)
        except NotFoundError:
            raise
        except Exception:
            pass

    def _locate_result(self, sel: Selector, ref: str) -> Any | None:
        cfg = self.config

        # If a row selector is configured (e.g. "tr.pro-to-track")
        if cfg.row_selector:
            rows = self._query(sel, cfg.row_selector)
            if rows:
                clean_ref = re.sub(r"\W", "", ref).lower()
                # Find row matching ref, or fallback to first row
                for r in rows:
                    row_txt = re.sub(r"\W", "", str(r.get_all_text().clean())).lower()
                    if clean_ref in row_txt:
                        return r
                return rows.first

        # If container selector is configured (e.g. "#trackingResults")
        if cfg.container_selector:
            containers = self._query(sel, cfg.container_selector)
            if containers:
                return containers.first

        # Fallback: look for common table rows or result cards
        common_rows = sel.css("tr.pro-to-track, .tracking-result, #trackingResults")
        if common_rows:
            return common_rows.first

        return None

    def _extract_table_row(self, root_sel: Selector, target_node: Any, ref: str) -> dict[str, Any]:
        """If target node is a <tr>, match <th> headers to <td> cells."""
        res: dict[str, Any] = {}
        row = target_node if target_node.tag == "tr" else None
        if not row:
            # check if target_node contains a <tr>
            tr_matches = target_node.css("tr")
            if tr_matches:
                row = tr_matches.first

        if not row:
            return res

        # Find parent table
        table_matches = row.xpath("ancestor::table")
        if not table_matches:
            return res

        table = table_matches.first
        headers: list[str] = []
        for th in table.css("th"):
            txt = str(th.get_all_text().clean()).lower()
            headers.append(txt)

        cells = row.css("td")
        for i, cell in enumerate(cells):
            cell_txt = str(cell.get_all_text().clean())
            if i < len(headers):
                hdr = headers[i]
                norm_field = HEADER_MAP.get(hdr)
                if norm_field and cell_txt:
                    res[norm_field] = cell_txt

        return res

    def _extract_field(self, node: Any, selector_expr: str) -> str | None:
        """Extract text from node using CSS or XPath selector."""
        try:
            matches = self._query(node, selector_expr)
            if not matches:
                return None
            first_match = matches.first
            txt = str(first_match.get_all_text().clean())
            return txt if txt not in ("-", "--", "N/A", "n/a", "None", "") else None
        except Exception as exc:
            logger.debug("Failed to extract field with %s: %s", selector_expr, exc)
            return None

    def _query(self, node: Any, selector_expr: str) -> Any:
        """Run CSS or XPath query on Scrapling Selector node."""
        s = selector_expr.strip()
        if s.startswith("/") or s.startswith(".//"):
            return node.xpath(s)
        return node.css(s)

    def _post_process(self, data: dict[str, Any], ref: str) -> None:
        """Clean values, parse dates, harmonize aliases."""
        if not data.get("reference"):
            data["reference"] = ref

        # Clean null-like strings
        for k in list(data.keys()):
            if data[k] in ("-", "--", "N/A", "n/a", "None", ""):
                data[k] = None

        # Parse dates
        if data.get("pickup_date"):
            data["pickup_date"] = parse_date_value(data["pickup_date"])
        if data.get("delivery_date"):
            data["delivery_date"] = parse_date_value(data["delivery_date"])
        if data.get("eta"):
            data["eta"] = parse_date_value(data["eta"])

        # Parse pieces and weight from amount string (e.g. "9 items x 1120 lbs")
        amount = data.get("amount")
        if amount and isinstance(amount, str):
            p_match = re.search(r"(\d+)\s*(?:item|piece|pc|pkg)s?", amount, re.IGNORECASE)
            if p_match and not data.get("pieces"):
                data["pieces"] = int(p_match.group(1))
            w_match = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:lb|lbs|kg|kgs)", amount, re.IGNORECASE)
            if w_match and not data.get("weight"):
                data["weight"] = float(w_match.group(1).replace(",", ""))

        # Harmonize destination/consignee and origin/shipper
        if data.get("consignee") and not data.get("destination"):
            data["destination"] = data["consignee"]
        elif data.get("destination") and not data.get("consignee"):
            data["consignee"] = data["destination"]

        if data.get("shipper") and not data.get("origin"):
            data["origin"] = data["shipper"]
        elif data.get("origin") and not data.get("shipper"):
            data["shipper"] = data["origin"]
