# AQUA Sentinel — API et interface locales V1

Cette livraison permet de consulter le réseau simulé, rejouer ses alertes et lire les priorités d'inspection sur actifs fictifs. Elle utilise la détection V3 et le risque V1 déjà évalués. Elle n'entraîne aucun modèle et ne produit aucun nouveau jeu de test.

## Fichiers à lire avant intégration

| Fichier | Rôle |
|---|---|
| `aqua_api_v1.py` | API FastAPI, contrôle des modèles, chargement des résultats, inférence et vérification HTTP |
| `aqua_interface_v1.html` | Interface en français : réseau, inspections et résultats ; HTML, CSS, JavaScript et SVG |
| `requirements_api_v1.txt` | Dépendances ajoutées pour l'API ; versions utilisées pour les essais locaux |
| `aqua_api_v1_verification.json` | Rapport des vérifications locales et limites de ces vérifications |
| `AQUA_API_V1_LIRE_AVANT.md` | Ce guide |

La page est servie par FastAPI. Elle charge les données depuis l'API locale. Aucun serveur Node, outil de compilation JavaScript, CDN ou police externe n'est nécessaire pour l'interface. La documentation technique `/docs` utilise les ressources habituelles de Swagger UI et peut nécessiter Internet.

## Prérequis dans ton projet Windows

Le dossier est `C:\Users\HP\Documents\aqua-sentinel`, avec son environnement `.venv` et les scripts AQUA précédents.

Ces deux étapes doivent être terminées :

- Détection V3 : un dossier `models\aqua_detection_v3\<identifiant>` contient `_GELE`, `_TEST_EVALUE`, `modele.json`, `gel.json`, `bilan_test.json` et `predictions_bilans13_test.csv`.
- Risque V1 : un dossier `models\aqua_risque_v1\<identifiant>` contient `_GELE`, `_TEST_EVALUE`, `modele.joblib`, `gel.json`, `bilan_test.json` et `illustration_maquette\observations_fictives.csv`.

Les données de capteurs V3 doivent rester à l'emplacement enregistré dans son rapport. Les modèles et scripts dont dépendent les gels doivent également rester disponibles. L'API refuse un modèle dont le code ou l'empreinte a changé ; elle ne relance pas les expériences pour le réparer.

## Installation

Télécharger `AQUA_API_V1.zip`, puis extraire son contenu **directement dans le dossier aqua-sentinel**. Les cinq fichiers ci-dessus doivent être à côté de `aqua_detection_v3.py` et `aqua_risque_v1.py`.

Dans PowerShell :

```powershell
& {
    Set-Location -LiteralPath 'C:\Users\HP\Documents\aqua-sentinel' -ErrorAction Stop

    .\.venv\Scripts\python.exe -m pip install -r .\requirements_api_v1.txt
    if ($LASTEXITCODE -ne 0) { throw "Installation API interrompue." }

    .\.venv\Scripts\python.exe .\aqua_api_v1.py verifier
    if ($LASTEXITCODE -ne 0) { throw "Verification API interrompue." }

    .\.venv\Scripts\python.exe .\aqua_api_v1.py servir --ouvrir
    if ($LASTEXITCODE -ne 0) { throw "Lancement API interrompu." }
}
```

Le navigateur s'ouvre quand le service répond. Sinon ouvrir **http://127.0.0.1:8012**. Garder la fenêtre PowerShell ouverte ; **Ctrl+C** arrête le service.

Si le port est déjà utilisé :

```powershell
.\.venv\Scripts\python.exe .\aqua_api_v1.py servir --port 8013 --ouvrir
```

Le service choisit le dernier modèle évalué de chaque famille. Pour sélectionner explicitement une exécution :

```powershell
.\.venv\Scripts\python.exe .\aqua_api_v1.py servir --ouvrir `
    --modele-detection '.\models\aqua_detection_v3\IDENTIFIANT_DETECTION' `
    --modele-risque '.\models\aqua_risque_v1\IDENTIFIANT_RISQUE'
```

## Ce que montrent les écrans

### Réseau

- Schéma du réservoir R, des points A à D et des conduites P1 à P5. Les positions sont graphiques et sans échelle.
- Choix parmi les 64 journées du test V3 déjà enregistré. Le choix initial prend un scénario contenant une alerte enregistrée, à son premier déclenchement.
- Curseur de 00:00 à 23:59 ; lecture accélérée à une minute simulée toutes les 0,5 seconde.
- Couleur d'alerte, conduite signalée, pression D et débit d'entrée à l'instant du curseur.
- Courbe de l'écart instantané entre les deux débits sur P2, P3, P4 ou P5. Cet écart n'est pas une mesure certaine du débit de fuite. Le détecteur emploie un lissage et une persistance.
- La courbe montre la journée complète enregistrée ; ce n'est pas une prévision des futures mesures.

« Pas d'alerte » signifie que le détecteur ne déclenche pas à cet instant. Cela ne garantit pas l'absence de fuite. Une alerte peut rester active après la fin de la fuite à cause de la règle de fermeture. La localisation désigne une conduite entre points de mesure et non la position précise du trou.

### Inspections

- Classement recalculé à partir du modèle risque figé et des neuf observations fictives sauvegardées.
- Cinq conduites P1–P5 et quatre raccords hypothétiques J_A–J_D ; ces raccords ne constituent pas un inventaire physique documenté.
- Indice relatif parmi les scores de train et contributions positives du modèle. Les contributions ne prouvent pas des causes physiques de casse.
- Quota de démonstration : les 20 % premiers de chaque date, arrondis au supérieur, soit deux actifs sur neuf. Même un lot sans danger connu recevrait deux propositions d'inspection.
- Un indice de 90 ne signifie pas 90 % de risque. Les âges, catégories de matériaux, états d'inspection et historiques de cette vue sont inventés.

Les neuf actifs de l'illustration ne sont pas les 2 000 actifs du test risque. Le test mesure la sélection d'incidents dans cette cohorte distincte ; il ne vérifie pas des ruptures de P5 ou J_B.

### Résultats

Résultats historiques de deux tests séparés :

- V3 : test de 64 journées simulées avec 32 fuites. Les résultats affichés sont lus dans le rapport de l'exécution choisie.
- Risque V1 : test de 2 000 actifs fictifs, classement pour 30 jours et budget de 400 inspections. Les résultats sont lus dans son rapport.

Ces comptes ne sont pas des mesures de performance terrain. Le bouton d'export télécharge le résumé JSON actuellement chargé.

## API disponible

Documentation interactive : http://127.0.0.1:8012/docs

| Méthode et chemin | Usage |
|---|---|
| `GET /api/v1/sante` | Vérifier que les données et modèles sont chargés |
| `GET /api/v1/resume` | Résultats historiques, limites et schéma du réseau |
| `GET /api/v1/scenarios` | Lister les journées enregistrées et leurs alertes |
| `GET /api/v1/scenarios/{scenario_id}` | Charger mesures et décisions de la journée |
| `GET /api/v1/priorites` | Lire les neuf priorités fictives recalculées |
| `POST /api/v1/detection/predire` | Appeler le détecteur V3 sur un historique simulé fourni |
| `POST /api/v1/risque/predire` | Appeler le risque V1 sur des observations fictives fournies |

### Contrat de détection

Corps JSON : `{"origine":"simulation","mesures":[...]}`. Chaque ligne suit le schéma V3 : `scenario_id`, `temps_s`, les huit débits amont/aval nécessaires de P2–P5 ; les cinq autres mesures historiques sont acceptées. Toutes les unités restent celles des scripts précédents : débits en m³/s en entrée du modèle, pressions en mètres.

Limite : 1 à 1 440 lignes, groupes continus au pas de 60 secondes, valeurs finies et clés uniques. Les colonnes de vérité, par exemple `fuite_active`, sont refusées.

**L'inférence est sans état persistant.** Chaque requête repart au début du lot fourni et recalcule lissage et persistance. Les deux premiers points sont neutres. Pour reproduire une décision de la journée, envoyer son historique depuis le début, jusqu'à cet instant. Des appels contenant seulement une nouvelle mesure ne forment pas un flux continu avec mémoire.

### Contrat de risque

Corps JSON : `{"origine":"simulation_actifs","observations":[...]}`. Les lignes suivent exactement `observations_fictives.csv` et le schéma de `aqua_risque_v1.py` ; 1 à 2 000 observations. Dates d'inspection futures, catégories inconnues, valeurs manquantes et colonnes de vérité sont refusées.

Le quota est appliqué séparément à chaque date d'observation, au lot fourni. Le rang et les propositions peuvent changer si le lot change. Le score interne et son indice relatif ne sont pas des probabilités de casse calibrées pour un réseau réel.

## Vérifications et portée

`verifier` contrôle les routes, les 64 replays, l'inférence de deux journées représentatives, le classement des neuf actifs, le refus d'entrées invalides et la conservation des modèles/résultats. Il écrit un rapport dans `resultats_aqua\api_v1\<identifiant>\verification.json`.

Le rapport livré détaille également les contrôles de logique JavaScript. Le contrôle visuel dans le navigateur Windows reste à confirmer : le navigateur distant utilisé pour le contrôle n'a pas pu ouvrir l'aperçu local. Les tests de logique ne remplacent pas cette vérification visuelle.

Cette V1 est un service local sans connexion à des capteurs réels ni commande d'équipement. Elle ne comporte pas encore d'authentification, de stockage opérationnel des nouvelles requêtes, de flux persistant ou de déploiement public.

## Sources

- Résultats et modèles figés produits par `aqua_detection_v3.py` et `aqua_risque_v1.py`, puis confirmés dans les sorties PowerShell de l'utilisateur.
- [FastAPI — cycle de vie de l'application](https://fastapi.tiangolo.com/advanced/events/).
- [FastAPI — corps des requêtes et validation](https://fastapi.tiangolo.com/tutorial/body/).
- [FastAPI — tests HTTP](https://fastapi.tiangolo.com/tutorial/testing/).
- [FastAPI — réponses HTML et fichiers](https://fastapi.tiangolo.com/advanced/custom-response/).
- [FastAPI 0.143.2 — PyPI](https://pypi.org/project/fastapi/0.143.2/).
- [Uvicorn 0.54.0 — PyPI](https://pypi.org/project/uvicorn/0.54.0/).
- [HTTPX 0.28.1 — PyPI](https://pypi.org/project/httpx/0.28.1/).
