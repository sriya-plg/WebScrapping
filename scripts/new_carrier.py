"""python scripts/new_carrier.py calix forwardair "Forward Air" -> app/clients/calix/carriers/forwardair/{config.yaml,adapter.py,hooks.py}"""
import pathlib, sys
client, folder, name = sys.argv[1].lower(), sys.argv[2].lower(), sys.argv[3] if len(sys.argv) > 3 else sys.argv[2]
root = pathlib.Path(__file__).resolve().parents[1]
d = root / "app/clients" / client / "carriers" / folder
d.mkdir(parents=True, exist_ok=True)
(d / "__init__.py").touch()
sub = {"__CLIENT__": client.title(), "__NAME__": name, "__CLS__": name.replace(" ", "").replace("_", "")}
def fill(t):
    for k, v in sub.items(): t = t.replace(k, v)
    return t
for out, tmpl in (("config.yaml", "carrier_config.yaml.tmpl"), ("adapter.py", "adapter.py.tmpl"), ("hooks.py", "hooks.py.tmpl")):
    (d / out).write_text(fill((root / "templates" / tmpl).read_text()))
print(f"created {d} -- fill the TODOs in config.yaml; nothing else to touch")
