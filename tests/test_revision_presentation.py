import csv
import json
import re
from io import BytesIO, StringIO
from sqlite3 import connect
from subprocess import run

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from service.api import create_app
from tests.fixtures import xlsx
from tests.test_acceptance import create, ok, upload
from tests.test_revision_materials import materials_pair


@pytest.mark.parametrize("offer_count", [1, 5])
def test_summary_and_structured_pdf_share_verified_values(tmp_path, offer_count):
    rows = [[f"B-{index:03}", "Deformed bar 16 mm Grade 60", "2", "pcs", "", ""]
            for index in range(28)]
    note = "Confirm the drawing revision.\nAsk whether the replacement is in stock."
    with TestClient(create_app(tmp_path / "workspace.sqlite")) as client:
        batch = create(client)
        route = f"/batches/{batch}"
        upload(client, batch, "Requirements.xlsx", xlsx(rows))
        for index in range(offer_count):
            material = "Deformed bar 12 mm Grade 60" if index == 0 else rows[0][1]
            quote_rows = [[row[0], material, *row[2:4], str(100 + index), str(2 * (100 + index))]
                          for row in rows]
            upload(client, batch, f"Offer-{index + 1}.xlsx", xlsx(quote_rows, [
                f"Reference: QUOTE-{index + 1}", "Tax: VAT inclusive", "Freight: Included",
                "Charges: None", "Discount: None", f"Total: {28 * 2 * (100 + index)}",
            ]), f"Supplier {index + 1} with a long name for the column wrapping check",
                coverage="partial" if index == 4 else "full")
        assert client.get(f"{route}/export/pdf").status_code == 409
        ok(client.patch(f"{route}/scope", json={"mode": "all"}))
        verified = ok(client.post(f"{route}/verify"))
        issue = next(item for item in verified["issues"] if item["rule"] == "dimension")
        assert issue["detail"]["expected"] == "16 mm"
        assert "12 mm" in issue["detail"]["observed"]
        assert "Supplier 1" in issue["detail"]["context"]
        ok(client.patch(f"{route}/issues/{issue['id']}", json={
            "status": "false_positive", "note": note, "fingerprint": issue["fingerprint"],
        }))
        loaded = ok(client.get(route))
        assert next(item for item in loaded["issues"] if item["id"] == issue["id"])["review"]["note"] == note
        summary = loaded["summary"]
        assert summary["scope"] == "All 28 material rows"
        assert len(summary["offers"]) == offer_count
        assert [offer["comparable"] for offer in summary["offers"]] == (
            [False] if offer_count == 1 else [True, True, True, False, False])
        first = next(offer for offer in summary["offers"] if offer["cells"]["reference"]["value"] == "QUOTE-1")
        assert first["cells"]["quoted_total"]["value"] == "₱5,600"
        assert first["cells"]["payable"]["value"] == "Incomparable"
        assert first["cells"]["freight"]["evidence"]
        assert issue["id"] not in first["cells"]["findings"]["findings"]

        exported = client.get(f"{route}/export/pdf")
        assert exported.is_success
        (tmp_path / "verification.pdf").write_bytes(exported.content)
        extracted = run(["pdftotext", "-layout", "-", "-"], input=exported.content,
                        capture_output=True, check=True).stdout.decode()
        pages = extracted.split("\f")
        assert "Supplier Quotation Summary" in pages[0]
        assert "Internal canvass" in pages[0]
        assert "QUOTE-1" in pages[0] and "5,600" in pages[0]
        for section in ("Scope and sources", "Findings", "Commercial terms and evidence", "Detailed line items", "Page 2"):
            assert section in extracted
        assert re.search(r"Expected or required +Offered or observed", extracted)
        assert re.search(r"Source and location +Evidence excerpt", extracted)
        assert re.search(r"Confirm the drawing revision\.\s*\n\s*Ask whether the replacement is in stock\.", extracted)
        assert "Materials B2" in extracted
        abstract = client.get(f"{route}/export/abstract")
        if offer_count == 1:
            assert "abstract" not in loaded and abstract.status_code == 409
        else:
            bids = loaded["abstract"]
            assert len(bids["items"]) == 28 and len(bids["bidders"]) == 5
            first_offer = next(bidder["document"] for bidder in bids["bidders"] if bidder["reference"] == "QUOTE-1")
            cheapest = next(bidder for bidder in bids["bidders"] if bidder["reference"] == "QUOTE-2")
            assert bids["lowest"] == cheapest["document"] and cheapest["rank"].startswith("1st")
            assert all(item["bids"][cheapest["document"]]["lowest"] for item in bids["items"])
            assert not any(item["bids"][first_offer]["lowest"] for item in bids["items"])  # mismatched size
            assert abstract.is_success and abstract.headers["content-type"] == "application/pdf"
            sheet = run(["pdftotext", "-layout", "-", "-"], input=abstract.content,
                        capture_output=True, check=True).stdout.decode()
            for label in ("ABSTRACT OF BIDS", "Total amount of bid (as calculated)", "1st · Lowest",
                          "Prepared by", "Approved by"):
                assert label in sheet
        machine_readable = client.get(f"{route}/export/csv").content.decode("utf-8-sig")
        header, *records = csv.reader(StringIO(machine_readable))
        assert all(len(row) == len(header) for row in records)
        kinds = [row[0] for row in records]
        assert kinds.index("Summary") < kinds.index("Issue") < kinds.index("Material")
        assert any(row[0] == "Issue" and row[-1] == note for row in records)


def test_summary_only_quote_displays_verified_basis_and_unresolved_costs(tmp_path, monkeypatch):
    monkeypatch.setenv("SURIQ_AI_PROVIDER", "off")
    with TestClient(create_app(tmp_path / "workspace.sqlite")) as client:
        batch = create(client)
        route = f"/batches/{batch}"
        for name, contents in zip(("Requirements.pdf", "Offer.pdf"), materials_pair()):
            ok(client.post(f"{route}/documents", files={"file": (name, contents)}))
        ok(client.patch(f"{route}/scope", json={"mode": "all"}))
        verified = ok(client.post(f"{route}/verify"))
        assert verified["summary"]["site"] == "Sual"
        offer, = verified["summary"]["offers"]
        cells = offer["cells"]
        assert "₱386,938.68 + ₱399.00 = ₱387,337.68" in cells["materials"]["value"]
        assert {anchor["source"] for anchor in cells["materials"]["evidence"]} == {1, 2}
        assert cells["quoted_materials"]["value"] == "₱387,337.68"
        assert cells["quoted_total"]["value"] == "₱513,538.98"
        assert cells["payable"]["value"] == "Incomparable"
        assert cells["coverage"]["value"].startswith("Summary only.")
        assert cells["tax"]["reasons"] == [
            "VAT basis unclear. The quote lists ₱0.00 VAT but does not state how VAT applies to the prices."
        ]
        assert cells["freight"]["reasons"] == ["Freight unstated. Confirm whether delivery is included or separately charged."]
        assert "10,000.00" in cells["mobilization"]["value"]
        assert "2026-11-28" in cells["validity"]["value"]
        exported = client.get(f"{route}/export/pdf")
        assert exported.is_success
        (tmp_path / "verification.pdf").write_bytes(exported.content)
        extracted = run(["pdftotext", "-layout", "-", "-"], input=exported.content,
                        capture_output=True, check=True).stdout.decode()
        assert "PHP 386,938.68 + PHP 399.00 = PHP 387,337.68" in extracted
        assert "PHP 513,538.98" in extracted and "Expiry · 2026-11-28" in extracted
        assert "Offer.pdf · p. 1" in extracted and "Requirements.pdf · p. 2" in extracted
        assert extracted.count("Summary only. Line coverage") == 1
        assert "Not itemized" not in extracted


@pytest.mark.parametrize("price,amount,currency,weak,display,note", [
    (50, 20, "PHP", False, "₱20", "Price needs verification"),
    (20, 40, "PHP", True, "₱40", "Price needs verification"),
    (30, 60, "USD", False, "USD 60", "Different currency"),
])
def test_abstract_highlights_only_verified_php_item_prices(tmp_path, price, amount, currency, weak, display, note):
    database = tmp_path / "workspace.sqlite"
    row = ["B-014", "Deformed bar 16 mm Grade 60", "2", "pcs"]
    with TestClient(create_app(database)) as client:
        batch = create(client)
        route = f"/batches/{batch}"
        upload(client, batch, "Requirements.xlsx", xlsx([[*row, "", ""]]))
        terms = ["Tax: VAT inclusive", "Freight: Included", "Charges: None", "Discount: None"]
        verified = upload(client, batch, "Verified.xlsx", xlsx([[*row, 50, 100]], [*terms, "Total: 100"]), "Verified")
        unresolved = upload(client, batch, "Unresolved.xlsx", xlsx([[*row, price, amount]], [
            *terms, f"Total: {amount}", f"Currency: {currency}",
        ]), "Unresolved")
        if weak:
            with connect(database) as db:
                snapshot_id, content = db.execute("SELECT id, content FROM snapshots WHERE document = ?",
                                                  (unresolved,)).fetchone()
                snapshot = json.loads(content)
                snapshot["lines"][0]["anchors"]["unit_price"]["weak"] = True
                db.execute("UPDATE snapshots SET content = ? WHERE id = ?", (json.dumps(snapshot), snapshot_id))
        ok(client.patch(f"{route}/scope", json={"mode": "all"}))
        data = ok(client.post(f"{route}/verify"))
        item, = data["abstract"]["items"]
        bids = item["bids"]
        assert all(bid["state"] == "pass" for bid in bids.values())
        assert {key for key, bid in bids.items() if bid["lowest"]} == {verified}
        assert bids[unresolved]["amount"] == display
        assert bids[unresolved]["price_note"] == note

        # Load a result saved before per-item prices carried a verification decision.
        with connect(database) as db:
            stored = json.loads(db.execute("SELECT result FROM batches WHERE id = ?", (batch,)).fetchone()[0])
            for comparison in stored["comparison"]:
                for result in comparison["items"].values():
                    result.pop("verified_amount", None)
            db.execute("UPDATE batches SET result = ? WHERE id = ?", (json.dumps(stored), batch))
        old_data = ok(client.get(route))
        old_bid = old_data["abstract"]["items"][0]["bids"][verified]
        assert old_bid["amount"] == "₱100"
        assert not old_bid["lowest"]


@pytest.mark.parametrize("price_format,total_format,currency,item_amount,as_read,eligible", [
    ('"USD "#,##0.00', '"USD "#,##0.00', "USD", "USD 40", "USD 40", False),
    ('"USD "#,##0.00', "[$₱-3409]#,##0.00", "USD", "USD 40", "₱40", False),
    ("[$₱-3409]#,##0.00", "[$PHP-3409]#,##0.00", "PHP", "₱40", "₱40", True),
    ("[$-409]#,##0.00", "[$-409]#,##0.00", "", "₱40", "₱40", True),
    ("[$$-409]#,##0.00", "$#,##0.00", "$", "$40", "$40", False),
])
def test_abstract_respects_xlsx_currency_formats(tmp_path, price_format, total_format, currency, item_amount, as_read, eligible):
    row = ["B-014", "Deformed bar 16 mm Grade 60", "2", "pcs"]
    terms = ["Tax: VAT inclusive", "Freight: Included", "Charges: None", "Discount: None"]
    with TestClient(create_app(tmp_path / "workspace.sqlite")) as client:
        batch = create(client)
        route = f"/batches/{batch}"
        upload(client, batch, "Requirements.xlsx", xlsx([[*row, "", ""]]))
        verified = upload(client, batch, "Verified.xlsx", xlsx([[*row, 50, 100]], [*terms, "Total: 100"]), "Verified")
        workbook = load_workbook(BytesIO(xlsx([[*row, 20, 40]], [*terms, "Total: 40"])))
        for cell in ("E2", "F2"):
            workbook.active[cell].number_format = price_format
        workbook.active["B7"].value = 40
        workbook.active["B7"].number_format = total_format
        blob = BytesIO()
        workbook.save(blob)
        formatted = upload(client, batch, "Formatted.xlsx", blob.getvalue(), "Formatted")

        ok(client.patch(f"{route}/scope", json={"mode": "all"}))
        verified_batch = ok(client.post(f"{route}/verify"))
        source = next(doc for doc in verified_batch["documents"] if doc["id"] == formatted)
        assert source["lines"][0]["anchors"]["amount"]["text"] == f"{currency} 40".strip()
        abstract = verified_batch["abstract"]
        bidder = next(bidder for bidder in abstract["bidders"] if bidder["document"] == formatted)
        assert bidder["as_read"] == as_read
        assert abstract["items"][0]["bids"][formatted]["amount"] == item_amount
        assert abstract["items"][0]["bids"][formatted]["lowest"] is eligible
        assert abstract["lowest"] == (formatted if eligible else verified)
        if item_amount != as_read:
            assert bidder["materials"] == ""


@pytest.mark.parametrize("amounts,display", [
    (("Infinity", "-Infinity"), ""),
    (("1,2", "30"), ""),
    (("20", "30"), "₱50"),
])
def test_abstract_aggregates_only_valid_duplicate_amounts(tmp_path, amounts, display):
    row = ["B-014", "Deformed bar 16 mm Grade 60", "2", "pcs"]
    with TestClient(create_app(tmp_path / "workspace.sqlite")) as client:
        batch = create(client)
        route = f"/batches/{batch}"
        upload(client, batch, "Requirements.xlsx", xlsx([[*row, "", ""]]))
        upload(client, batch, "Other.xlsx", xlsx([[*row, 20, 40]]), "Other")
        duplicate = upload(client, batch, "Duplicate.xlsx", xlsx([[*row, 10, amount] for amount in amounts]), "Duplicate")
        ok(client.patch(f"{route}/scope", json={"mode": "all"}))
        bid = ok(client.post(f"{route}/verify"))["abstract"]["items"][0]["bids"][duplicate]
        assert bid["amount"] == display
        assert not bid["lowest"]


def test_abstract_shares_ranks_for_equal_comparable_totals(tmp_path):
    row = ["B-014", "Deformed bar 16 mm Grade 60", "2", "pcs"]
    with TestClient(create_app(tmp_path / "workspace.sqlite")) as client:
        batch = create(client)
        route = f"/batches/{batch}"
        upload(client, batch, "Requirements.xlsx", xlsx([[*row, "", ""]]))
        quotes = {}
        for name, price in [("Alpha", 50), ("Beta", 50), ("Gamma", 60)]:
            quotes[name] = upload(client, batch, f"{name}.xlsx", xlsx([[*row, price, 2 * price]], [
                f"Reference: QUOTE-{name}", "Tax: VAT inclusive", "Freight: Included", "Charges: None",
                "Discount: None", f"Total: {2 * price}",
            ]), name)
        ok(client.patch(f"{route}/scope", json={"mode": "all"}))
        abstract = ok(client.post(f"{route}/verify"))["abstract"]
        assert abstract["lowest"] is None
        assert set(abstract["lowest_documents"]) == {quotes["Alpha"], quotes["Beta"]}
        assert [bidder["rank"] for bidder in abstract["bidders"]] == ["1st · Tied lowest", "1st · Tied lowest", "3rd"]
        assert "2 bids are tied" in abstract["recommendation"]
        assert all(name not in abstract["recommendation"] for name in quotes)
        assert {key for key, bid in abstract["items"][0]["bids"].items() if bid["lowest"]} == set(abstract["lowest_documents"])

        pdf = client.get(f"{route}/export/abstract")
        assert pdf.is_success
        sheet = run(["pdftotext", "-layout", "-", "-"], input=pdf.content, capture_output=True, check=True).stdout.decode()
        assert sheet.count("1st · Tied lowest") == 2
        assert "2 bids are tied" in sheet
