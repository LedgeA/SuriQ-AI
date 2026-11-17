"""Shared presentation of stored verification results for the workspace and PDF."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from itertools import groupby
import re
from zoneinfo import ZoneInfo

from service.boq import is_boq, material_line
from service.commercial import currency_code, monetary_value


COVERAGE = {"full": "Full scope", "partial": "Partial offer", "unknown": "Coverage unknown"}
REVIEW = {"open": "Open", "confirmed": "Confirmed", "false_positive": "False positive",
          "clarification": "Needs clarification"}
MATERIAL_FIELDS = {"dimension", "grade", "standard", "brand", "specification", "material", "quantity", "unit", "pack"}
MATERIAL_RULES = MATERIAL_FIELDS | {"missing", "extra", "match", "duplicate", "line_math", "subtotal",
                                     "outside_scope", "materials_summary", "materials_reconciliation", "work_scope"}
SUMMARY_FIELDS = {
    "reference": "Quote reference", "quote_date": "Quote date",
    "quoted_materials": "Quoted materials total", "quoted_total": "Quoted project or offer total",
    "payable": "Comparable payable total", "coverage": "Coverage", "materials": "Materials reconciliation",
    "tax": "VAT basis", "freight": "Freight", "handling": "Handling", "charges": "Additional charges",
    "discount": "Discount", "mobilization": "Mobilization and consumables",
    "labor_equipment": "Labor and equipment", "delivery": "Delivery", "validity": "Validity",
    "payment": "Payment", "warranty": "Warranty", "exclusions": "Exclusions", "findings": "Open material findings",
    "confirmed_blockers": "Confirmed blockers",
}


def supplier_label(name, filename):
    if not name or re.fullmatch(r"unidentified supplier(?: [0-9a-f]{6})?", name, re.I):
        return f"Supplier name needed ({filename})"
    return name


def source_label(anchor):
    if anchor.get("scope") == "document":
        return "Document reference"
    if isinstance(anchor["source"], int):
        return f"p. {anchor['source']}"
    return f"{anchor['source']} {', '.join(anchor.get('cells', []))}".strip()


def material_label(fields):
    name = fields.get("material") or fields.get("description", "")
    return " · ".join(value for value in (fields.get("code"), name) if value)


def money_display(value, currency="PHP"):
    if value is None or value == "":
        return ""
    raw = value.strip()
    match = re.fullmatch(r"(?:(₱|US\$|\$|[A-Z]{3})\.?\s*)?(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)", raw, re.I)
    if not match:
        return raw
    code = currency_code(match[1] or currency)
    prefix = {"PHP": "₱", "$": "$"}.get(code, f"{code} ")
    number = Decimal(match[2].replace(",", ""))
    return f"{'-' if number < 0 else ''}{prefix}{number.copy_abs():,f}"


def manila_stamp(value):
    if not value:
        return ""
    return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Manila")).strftime("%d %b %Y, %H:%M:%S Asia/Manila")


def finding_details(batch, docs):
    lines = {(doc["id"], line["id"]): material_line(line)["fields"] if is_boq(doc) else line["fields"]
             for doc in docs.values() for line in doc["lines"]}
    baseline = docs.get(batch.get("baseline_document")) or next(
        (doc for doc in docs.values() if doc["role"] in {"bom", "boq"}), None)
    materials = {line["id"]: lines[(baseline["id"], line["id"])] for line in baseline["lines"]} if baseline else {}
    matches = defaultdict(list)
    for locator, match in batch["matches"].items():
        doc, line = locator.split(":", 1)
        matches[(match["bom"], doc)].append(lines.get((doc, line), {}))

    for issue in batch["issues"]:
        detail = issue.get("detail") or {}
        subject, condition, rule = issue["subject"], issue["condition"], issue["rule"]
        material = materials.get(subject, {}) if isinstance(subject, str) else lines.get(tuple(subject), {})
        sources = list(dict.fromkeys(anchor["document"] for anchor in issue["evidence"]))
        suppliers = [key for key in sources if key in docs and docs[key]["role"] == "quote"]
        names = {key: supplier_label(docs[key]["supplier"], docs[key]["name"]) for key in suppliers}
        context = " · ".join(value for value in (material_label(material), ", ".join(dict.fromkeys(names.values()))) if value)
        labels = {}
        for key in sources:
            role = docs.get(key, {}).get("role")
            labels[key] = ("Required" if role in {"bom", "boq", "buyer"} else
                           "Offered" if role == "quote" else "Reference")

        expected, observed = "", ""
        if rule in MATERIAL_FIELDS and isinstance(subject, str):
            expected = field_value(material, rule)
            offered = {key: list(dict.fromkeys(filter(None, (field_value(fields, rule)
                       for fields in matches[(subject, key)])))) for key in suppliers}
            offered = {key: ", ".join(values) for key, values in offered.items() if values}
            observed = "\n".join(f"{names[key]} · {value}" if len(offered) > 1 else value
                                 for key, value in offered.items())
            for key, value in offered.items():
                labels[key] = f"Offered · {value}"
            if expected:
                labels.update({key: f"Required · {expected}" for key in sources if labels[key] == "Required"})
        if isinstance(condition, list) and len(condition) == 2 and rule in (
            MATERIAL_FIELDS | {"line_math", "subtotal", "total", "delivery", "materials_summary"}
        ):
            expected = expected or str(condition[0])
            observed = observed or str(condition[1])
        if rule == "missing":
            expected, observed = material_label(material), "No matching item quoted"
        if rule in {"extra", "match", "outside_scope"}:
            observed = material_label(material) or observed
        if rule == "terms" and suppliers:
            offered_terms = docs[suppliers[0]].get("term_mentions", {}).get(str(condition), [])
            observed = " / ".join(dict.fromkeys(mention["value"] for mention in offered_terms))
        if not observed and issue.get("source_editors"):
            editor = issue["source_editors"][0]
            if editor["target"] == "terms":
                observed = docs.get(editor["document"], {}).get("terms", {}).get(editor["field"], "")

        issue["detail"] = {
            "context": context or ", ".join(docs[key]["name"] for key in sources if key in docs),
            "expected": expected, "observed": observed, "reason": issue["note"],
            "action": ("Check the cited source and link the correct reading." if issue.get("source_editors") else
                       "Compare the cited values and record a review decision."),
            **detail,
            "source_labels": {**labels, **detail.get("source_labels", {})},
        }


def field_value(fields, key):
    value = fields.get(key, "")
    if key == "material":
        return value or fields.get("description", "")
    if key == "quantity" and value:
        return f"{value} {fields.get('unit', '')}".strip()
    return value


def cell(value="", evidence=(), findings=(), reasons=()):
    return {"value": value, "evidence": list(evidence), "findings": list(dict.fromkeys(filter(None, findings))),
            "reasons": list(dict.fromkeys(filter(None, reasons)))}


def term_source(doc, field):
    source = doc["term_anchors"].get(field)
    return [{**source, "document": doc["id"]}] if source else []


def term_cell(term, doc, field):
    if not term:
        return cell(doc["terms"].get(field, ""), term_source(doc, field))
    required, offered = term.get("required"), term.get("offered")
    if required:
        value = f"{term['status']}\nRequired · {required}\nOffered · {offered or 'Unstated'}"
    elif term["status"] == "Needs clarification":
        value = f"Needs clarification\nOffered · {offered or 'Unstated'}"
    else:
        value = offered or ""
    return cell(value, term.get("evidence", []), [term.get("finding")], [term.get("reason")])


def merge_cells(first, second):
    return cell("\n".join(dict.fromkeys(filter(None, (first["value"], second["value"])))),
                [*first["evidence"], *second["evidence"]], [*first["findings"], *second["findings"]],
                [*first["reasons"], *second["reasons"]])


def materials_cell(result):
    if not result:
        return cell()
    parts = [result["status"]]
    currency = result.get("currency")
    if currency and result.get("subtotal") is not None and result.get("expected") is not None:
        if result["arithmetic"] == "Verified":
            calculation = money_display(result["subtotal"], currency)
            for item in result["adjustments"]:
                amount = money_display(item["value"], currency)
                calculation += f" - {amount[1:]}" if amount.startswith("-") else f" + {amount}"
            parts.append(f"{calculation} = {money_display(result['expected'], currency)}")
    if currency and result.get("expected") is not None:
        parts.append(f"Baseline materials · {money_display(result['expected'], currency)}")
    if currency and result.get("offered") is not None:
        parts.append(f"Quoted materials · {money_display(result['offered'], currency)}")
    parts.append(f"Underlying arithmetic · {result['arithmetic'].lower()}")
    return cell("\n".join(parts), result["evidence"], [result.get("finding")], [result.get("reason")])


def offer_column(row, doc, issues):
    commercial = row.get("commercial") or {}
    terms = {term["field"]: term for term in commercial.get("terms", [])}
    costs = {cost["field"]: cost for cost in commercial.get("costs", [])}
    comparable = row["total"] is not None
    summary_only = doc.get("summary_only")
    cells = {}
    for field in ("reference", "quote_date", "delivery", "payment", "warranty", "exclusions"):
        cells[field] = term_cell(terms.get(field), doc, field)
    if not cells["payment"]["value"]:
        milestones = [(doc["terms"][key], term_source(doc, key)) for key in ("advance_payment", "balance_payment")
                      if doc["terms"].get(key)]
        if milestones:
            cells["payment"] = cell("\n".join(value for value, _ in milestones),
                                    [anchor for _, anchors in milestones for anchor in anchors])
    for label, field in (("quoted_materials", "materials_total"), ("quoted_total", "total")):
        cells[label] = cell(money_display(doc["terms"].get(field), row["currency"]), term_source(doc, field))

    cost_rules = {"tax", "charges", "subtotal", "total", "supplier_duplicate", "line_math"}
    if commercial.get("validity", {}).get("status") == "Expired":
        cost_rules.add("validity")
    cost_findings = [issue["id"] for issue in issues if issue["rule"] in cost_rules]
    payable_evidence = [anchor for field in ("subtotal", "total", "tax", "vat", "freight", "handling", "charges", "discount")
                        for anchor in term_source(doc, field)]
    reasons = ["Itemized coverage is required for a comparable payable total."
               if summary_only and reason.startswith("Summary quotation.") else reason for reason in row["reasons"]]
    cells["payable"] = cell(money_display(row["total"], row["currency"]) if comparable else "Incomparable",
                            evidence=payable_evidence, findings=[] if comparable else cost_findings, reasons=reasons)
    cells["coverage"] = cell("Summary only. Line coverage, quantities and specifications are unverified."
                             if summary_only else COVERAGE[row["coverage"]])
    cells["materials"] = materials_cell(row.get("materials"))

    for field in ("tax", "freight", "handling", "charges", "discount", "mobilization", "labor_equipment"):
        cost = costs.get("vat" if field == "tax" else field)
        if not cost:
            value = row["tax_basis"] if field == "tax" else doc["terms"].get(field, "")
            cells[field] = cell(value, term_source(doc, field))
        else:
            value = cost.get("value")
            parts = [cost["status"]]
            if value and value.casefold() != cost["status"].casefold():
                parts.append(money_display(value, row["currency"]))
            if field == "tax":
                if cost.get("quoted_amount"):
                    parts.append(f"Quoted VAT · {money_display(cost['quoted_amount'], row['currency'])}")
                if (treatment := cost.get("price_treatment")) and treatment != cost["status"]:
                    parts.append(f"Price treatment · {treatment}")
                if cost.get("declaration"):
                    parts.append(f"Declaration · {cost['declaration']}")
            cells[field] = cell("\n".join(parts), cost.get("evidence", []), [cost.get("finding")], [cost.get("reason")])
        if terms.get(field, {}).get("required"):
            cells[field] = merge_cells(cells[field], term_cell(terms[field], doc, field))

    validity = commercial.get("validity") or {}
    if issue_date := validity.get("issue_date"):
        date_cell = cells["quote_date"]
        date_cell["value"] = "\n".join(dict.fromkeys(filter(None, (issue_date, date_cell["value"]))))
    stated = doc["terms"].get("validity")
    expiry = validity.get("expiry") or row.get("expiry")
    parts = list(filter(None, (validity.get("status"), stated, f"Expiry · {expiry}" if expiry else "")))
    cells["validity"] = cell("\n".join(parts), validity.get("evidence") or term_source(doc, "validity"),
                             [validity.get("finding")], [validity.get("reason")])
    if terms.get("validity", {}).get("required"):
        cells["validity"] = merge_cells(cells["validity"], term_cell(terms["validity"], doc, "validity"))
    open_findings = [issue["id"] for issue in issues if issue["rule"] in MATERIAL_RULES
                     and issue["severity"] != "Info" and issue["review"]["status"] in {"open", "clarification"}]
    cells["findings"] = cell(str(len(open_findings)) if open_findings else "None", findings=open_findings)
    confirmed = [issue["id"] for issue in issues if issue["severity"] == "Blocking"
                 and issue["review"]["status"] == "confirmed"]
    cells["confirmed_blockers"] = cell(str(len(confirmed)) if confirmed else "", findings=confirmed)
    return {"document": doc["id"], "supplier": supplier_label(row["supplier"], doc["name"]),
            "comparable": comparable, "cells": cells}


def add_presentation(batch):
    docs = {doc["id"]: doc for doc in batch["documents"]}
    finding_details(batch, docs)
    if not batch["comparison"]:
        return
    by_offer = defaultdict(list)
    for issue in batch["issues"]:
        for document in dict.fromkeys(anchor["document"] for anchor in issue["evidence"]):
            by_offer[document].append(issue)
    offers = [offer_column(row, docs[row["document"]], by_offer[row["document"]]) for row in batch["comparison"]]
    offers.sort(key=lambda offer: (not offer["comparable"], offer["supplier"].casefold(),
                                   offer["cells"]["reference"]["value"].casefold()))
    baseline = docs.get(batch.get("baseline_document")) or next(
        (doc for doc in docs.values() if doc["role"] in {"bom", "boq"}), None)
    scope, site, exclusions = "", "", 0
    if baseline:
        rows = [line for line in baseline["lines"] if not line.get("excluded") and line.get("kind") not in {"service", "work"}
                and (batch["scope_mode"] == "all" or line["id"] in batch["scope"])]
        exclusions = sum(bool(line.get("excluded") or line.get("kind") in {"service", "work"}) for line in baseline["lines"])
        count = len(rows)
        scope = f"{'All' if batch['scope_mode'] == 'all' else 'Selected'} {count} material {'row' if count == 1 else 'rows'}"
        site = baseline["terms"].get("project_site", "")
        if not site:
            schedule = rows[0].get("schedule", "") if rows else ""
            if " · " in schedule and not schedule.startswith("Unidentified schedule"):
                site = schedule.rsplit(" · ", 1)[-1]
    optional = {"handling", "mobilization", "labor_equipment", "materials", "confirmed_blockers"}
    fields = [{"key": key, "label": label} for key, label in SUMMARY_FIELDS.items() if key not in optional
              or any(offer["cells"][key]["value"] for offer in offers)]
    batch["summary"] = {"project": batch["project_name"], "site": site, "scope": scope, "exclusions": exclusions,
                        "verified": manila_stamp(batch["verified_at"]), "fields": fields, "offers": offers}
    if baseline and len(offers) > 1:
        batch["abstract"] = abstract_of_bids(batch, baseline, rows, offers)


def abstract_of_bids(batch, baseline, rows, offers):
    """Itemized Abstract of Bids (as calculated): baseline items x supplier unit price and amount."""
    comparison = {row["document"]: row for row in batch["comparison"]}
    boq = is_boq(baseline)
    items = []
    for number, line in enumerate(rows, 1):
        fields = material_line(line)["fields"] if boq else line["fields"]
        bids = {}
        for offer in offers:
            row = comparison[offer["document"]]
            item = row["items"].get(line["id"], {})
            quoted = item.get("offers") or []
            if len(quoted) == 1:
                shown_amount = quoted[0].get("amount")
            else:
                amounts = [monetary_value(entry.get("amount") or "") for entry in quoted]
                same_currency = all(amount and amount[0] in {"", row["currency"]} for amount in amounts)
                shown_amount = str(sum(amount[1] for amount in amounts)) if quoted and same_currency else None
            verified = monetary_value(item.get("verified_amount") or "")
            value = verified[1] if verified else None
            state = item.get("state", "not offered")
            price_note = ""
            if state == "pass" and value is None:
                price_note = "Different currency" if row["currency"] != "PHP" else "Price needs verification"
            bids[offer["document"]] = {
                "state": state, "price_note": price_note,
                "unit_price": "\n".join(dict.fromkeys(filter(None, (
                    money_display(entry.get("unit_price"), row["currency"]) for entry in quoted)))),
                "amount": money_display(shown_amount, row["currency"]),
                "value": value,
            }
        lowest_value = min((bid["value"] for bid in bids.values() if bid["value"] is not None), default=None)
        for bid in bids.values():
            value = bid.pop("value")
            bid["lowest"] = value is not None and value == lowest_value
        items.append({"number": number, "id": line["id"],
                      "description": " · ".join(filter(None, (fields.get("code"), fields.get("material")
                                                               or fields.get("description"), fields.get("specification")))),
                      "quantity": fields.get("quantity", ""), "unit": fields.get("unit", ""), "bids": bids})

    ranked = [(Decimal(comparison[offer["document"]]["total"]), offer["document"])
              for offer in offers if offer["comparable"]]
    ranked.sort(key=lambda entry: entry[0])
    rank, tied = {}, set()
    for _, equal_bids in groupby(ranked, key=lambda entry: entry[0]):
        documents = [document for _, document in equal_bids]
        place = len(rank) + 1
        rank.update(dict.fromkeys(documents, place))
        if len(documents) > 1:
            tied.add(place)
    lowest_documents = [document for document, place in rank.items() if place == 1]
    bidders = []
    for offer in offers:
        row, cells = comparison[offer["document"]], offer["cells"]
        place = rank.get(offer["document"])
        remarks = list(cells["payable"]["reasons"])
        if (count := len(cells["findings"]["findings"])):
            remarks.append(f"{count} open material {'finding' if count == 1 else 'findings'}")
        rank_label = "Not ranked"
        if place:
            suffix = "th" if 11 <= place % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(place % 10, "th")
            rank_label = f"{place}{suffix}"
            if place == 1:
                rank_label += " · Tied lowest" if place in tied else " · Lowest calculated bid"
            elif place in tied:
                rank_label += " · Tied"
        bidders.append({
            "document": offer["document"], "supplier": offer["supplier"],
            "reference": cells["reference"]["value"], "quote_date": cells["quote_date"]["value"],
            "materials": money_display(row["line_sum"], row["currency"]),
            "as_read": cells["quoted_total"]["value"],
            "as_calculated": cells["payable"]["value"],
            "coverage": cells["coverage"]["value"], "vat": row["tax_basis"],
            "delivery": cells["delivery"]["value"], "validity": cells["validity"]["value"],
            "payment": cells["payment"]["value"],
            "rank": rank_label,
            "remarks": remarks,
        })
    lowest = lowest_documents[0] if len(lowest_documents) == 1 else None
    if not lowest_documents:
        recommendation = "No bid is ranked: none of the offers has a comparable payable total."
    else:
        first = next(bidder for bidder in bidders if bidder["document"] == lowest_documents[0])
        if lowest:
            recommendation = f"{first['supplier']} has the lowest calculated comparable bid at {first['as_calculated']}."
        else:
            recommendation = (f"{len(lowest_documents)} bids are tied for the lowest calculated comparable bid "
                              f"at {first['as_calculated']}.")
        recommendation += " Award is subject to review of open findings and approval."
    return {"project": batch["project_name"], "batch": batch["name"],
            "site": batch["summary"]["site"], "scope": batch["summary"]["scope"],
            "prepared": manila_stamp(batch["verified_at"]), "items": items, "bidders": bidders,
            "lowest": lowest, "lowest_documents": lowest_documents, "recommendation": recommendation}
