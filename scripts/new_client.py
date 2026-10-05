"""python scripts/new_client.py acme ACME01 "Acme"  ->  app/clients/acme/{client.yaml, carriers/}"""
import pathlib, sys
key, bill_to = sys.argv[1].lower(), sys.argv[2].upper()
name = sys.argv[3] if len(sys.argv) > 3 else sys.argv[1]
d = pathlib.Path(__file__).resolve().parents[1] / "app/clients" / key
(d / "carriers").mkdir(parents=True, exist_ok=True)
(d / "__init__.py").touch(); (d / "carriers/__init__.py").touch()
(d / "client.yaml").write_text(f"""name: {name}
bill_to: [{bill_to}]
defaults:                       # shared by ALL {name} carriers; a carrier's mapping.yaml may override
  pending:                      # how to call/parse this client's "pending housebills" API
    endpoint: /{key}/housebills/pending?carrier={{carrier_code}}
    items_path: data
    ref_field: proNumber
    search_by_path: searchBy
    search_by_default: PRO
  payload_map:                  # common schema -> keys this client's backend expects
    billTo: ctx.bill_to
    carrierCode: ctx.carrier_code
    proNumber: reference
    status: status
    delivered: delivered
    deliveryDate: delivery_date
    events: events
    scrapedAt: scraped_at
""")
print(f"created {d}. Next: python scripts/new_carrier.py {key} <carrier>")
