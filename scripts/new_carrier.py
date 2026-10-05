"""python scripts/new_carrier.py calix forwardair [--adapter]  ->  carriers/forwardair/mapping.yaml (+ adapter.py)"""
import pathlib, sys
client, folder = sys.argv[1].lower(), sys.argv[2].lower()
d = pathlib.Path(__file__).resolve().parents[1] / "app/clients" / client / "carriers" / folder
d.mkdir(parents=True, exist_ok=True)
(d / "__init__.py").touch()
(d / "mapping.yaml").write_text(f"""# {client} x {folder}. All optional -- probe first: python scripts/probe.py {client} {folder} <ref>
aliases: []
# fields: {{status: "shipment.status"}}      # pin verified raw-JSON paths
# status_map: {{}}                           # only for statuses the built-in rules don't understand
# site: {{}}                                 # input_selector / submit_selector / response_regex / transport ...
""")
if "--adapter" in sys.argv:
    (d / "adapter.py").write_text('''"""Custom behaviour for this carrier ONLY. Override the smallest hook you need."""
from app.core.adapter import GenericAdapter


class CustomAdapter(GenericAdapter):
    async def _on_landing(self) -> None:       # e.g. dismiss a banner, clear a challenge, log in
        await super()._on_landing()
''')
print(f"created {d}" + ("" if "--adapter" in sys.argv else "  (no adapter needed unless the site is unusual)"))
