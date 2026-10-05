"""python scripts/new_client.py acme ACME01 "Acme"   -> app/clients/acme/{client.yaml, carriers/}"""
import pathlib, sys
key, bill_to, name = sys.argv[1].lower(), sys.argv[2].upper(), sys.argv[3] if len(sys.argv) > 3 else sys.argv[1]
d = pathlib.Path(__file__).resolve().parents[1] / "app/clients" / key
(d / "carriers").mkdir(parents=True, exist_ok=True)
(d / "__init__.py").touch(); (d / "carriers/__init__.py").touch()
src = (d.parent / "calix/client.yaml").read_text().replace("name: Calix", f"name: {name}").replace("[CALIX]", f"[{bill_to}]").replace("/calix/", f"/{key}/")
(d / "client.yaml").write_text(src)
print(f"created {d}; now: python scripts/new_carrier.py {key} <carrier> \"<Name>\"")
