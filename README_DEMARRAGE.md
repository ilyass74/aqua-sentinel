# AQUA Sentinel - premiere etape

Objectif : construire un reseau virtuel et comparer 24 heures sans fuite a
24 heures avec une fuite sur P4, entre 12 h et 18 h de temps simule.

Ce dossier contient le script autonome deja execute, ainsi que les versions
des dependances de cette execution. Les dimensions et consommations sont
des valeurs d'exercice. Les fichiers produits seront des donnees simulees.

## 1. Ouvrir le dossier dans PowerShell

Decompresser cette archive dans Documents : elle contient un dossier
`aqua-sentinel` avec le code et ce guide. Depuis PowerShell :

```powershell
$aquaProjectPath = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'aqua-sentinel'
Set-Location $aquaProjectPath
python --version
py -0p
git --version
```

Verifier la version et le chemin de Python avant l'installation. Les versions
de dependances ci-jointes ont ete executees sous Python 3.12.14, Linux 64 bits.
L'execution sur le PC Windows reste a verifier. Si `py` est absent, le message
sera utile pour choisir l'interpreteur disponible ; aucune installation de
Python n'est automatique.

## 2. Creer l'environnement avec Python 3.12

Lorsque `py -0p` confirme qu'un Python 3.12 64 bits est disponible :

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "import wntr; print('WNTR', wntr.__version__)"
```

Les commandes utilisent directement l'interpreteur de l'environnement.
L'activation et une modification de la politique PowerShell ne sont pas
necessaires.

## 3. Lancer la premiere simulation

```powershell
.\.venv\Scripts\python.exe .\aqua_sentinel_demo.py
Get-ChildItem .\resultats_aqua
Start-Process .\resultats_aqua\reseau.png
Start-Process .\resultats_aqua\comparaison.png
```

Resultats attendus :

- `mesures_normal.csv` : 1 441 lignes, de 0 a 86 400 secondes incluses.
- `mesures_fuite.csv` : memes capteurs et memes horodatages.
- `verite_evaluation/fuite_P4.csv` : statut de fuite et debit de fuite
  simule, reserves a l'evaluation.
- `reseau.png` et `comparaison.png` : structure et comparaison des signaux.
- `resume.json` : parametres, versions et resume des controles physiques.

Les CSV de mesures contiennent les pressions de A, B, C, D et les debits de
P1 a P5. Les pressions sont en metres de colonne d'eau et les debits en m3/s.
Le graphique exprime le debit de P1 en L/s. P4 est mesure sur sa partie
B-L4 ; l'autre partie L4-D est nommee P4_aval. L4 est un point de fuite,
pas un capteur observe par le futur modele.

Une nouvelle execution regenere les resultats de cet exercice dans le meme
dossier. Conserver une copie avant de modifier les parametres pour comparer
plusieurs experiences.

## 4. Lire et expliquer

- `creer_reseau` definit la source, les jonctions, les tuyaux et le scenario.
- `simuler` calcule pression/debit et verifie la conservation du debit.
- `tracer` genere les figures.
- `main` compare les deux cas, controle les horaires et exporte les fichiers.

Le premier jalon est valide quand le script termine avec
`"verifications": "PASS"`, que les fichiers existent et que les differences
entre les deux scenarios sont visibles pendant la fuite.

## Sources techniques

- WNTR, construction : https://usepa.github.io/WNTR/waternetworkmodel.html
- WNTR, hydraulique : https://usepa.github.io/WNTR/hydraulics.html
- WNTR, fuites : https://usepa.github.io/WNTR/disaster_models.html
- WNTR, unites : https://usepa.github.io/WNTR/units.html
- WNTR, installation : https://usepa.github.io/WNTR/installation.html
- Python, venv : https://docs.python.org/3.12/library/venv.html
