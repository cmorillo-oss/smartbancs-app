"""Resume el CPU por contenedor SOLO durante la carga (filas en que la API estaba activa, >20% de CPU)."""
import collections
import csv
import sys

rows = collections.defaultdict(dict)
for ts, name, cpu, _mem in csv.reader(open(sys.argv[1], encoding="utf-8")):
    rows[int(ts)][name.replace("smartbancs_app-", "").removesuffix("-1")] = float(cpu.strip("%"))
active = [t for t, v in rows.items() if v.get("transaction-api", 0) > 20]
svc = collections.defaultdict(list)
for t in active:
    for n, v in rows[t].items():
        svc[n].append(v)
for n, v in sorted(svc.items(), key=lambda x: -max(x[1])):
    if not n.startswith("tests-run"):
        print(f"  {n:<16} pico {max(v):6.1f}%   media {sum(v) / len(v):6.1f}%")
