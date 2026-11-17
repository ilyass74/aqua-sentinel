# AQUA Sentinel

AQUA Sentinel est un prototype de supervision et de detection de fuites sur un reseau d'eau simule. La V3 propose une interface de supervision, un suivi des capteurs et des interventions, ainsi que des essais de manoeuvre de vannes dans le simulateur.

## Demarrer sous Windows

Depuis la racine du depot, avec Python 3.12 et PowerShell :

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements_api_v3.txt
.\.venv\Scripts\python.exe aqua_api_v3.py verifier
.\.venv\Scripts\python.exe aqua_api_v3.py servir --ouvrir
```

L'interface est accessible sur [localhost:8014](http://127.0.0.1:8014). Le [guide V3](AQUA_V3_GUIDE.md) decrit les parcours de demonstration et l'utilisation des exemples. Le [guide initial](README_DEMARRAGE.md) couvre la simulation V1.

Les fichiers de `data/` et `models/` accompagnent les experiences sur donnees simulees. Les sorties locales sont generees dans `resultats_aqua/`.
