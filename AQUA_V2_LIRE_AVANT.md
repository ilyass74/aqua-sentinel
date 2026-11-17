# AQUA Sentinel V2 - six extensions et vannes modulantes simulees

Cette livraison ajoute les six fonctions decidees et un laboratoire de regulation. Le code est fourni pour lecture avant integration. Toutes les donnees restent simulees. Aucun equipement physique n'est commande.

## Ce qui est ajoute

| Fonction | Implementation livree | Verification |
|---|---|---|
| Simulateur d'intervention | Comparer maintien, reduction d'ouverture, isolement du troncon signale et isolement de l'autre zone. | Fuites de 4, 6, 8 et 12 mm dans chacune des deux zones ; conservation de la masse et arret de la fuite lors de l'isolement cible. |
| Surveillance des capteurs | Absence, horodatage decale, valeur repetee suspecte, plage de demonstration et trou temporel. | Les bilans concernes deviennent indetermines ; une alerte deja ouverte n'est pas oubliee pendant la panne. |
| Estimation des volumes | Integration trapezoidale des bilans signes sur les intervalles valides, avec corrections de soutirage/stockage si elles sont fournies. | 1 L/s pendant une heure donne 3,6 m3 ; un soutirage connu est soustrait. |
| Historique des interventions | Base SQLite, incidents par conduite, etat du signal, statut de suivi et commentaires conserves. | Reouverture de la base, absence de doublons, refus des conflits et ecritures concurrentes. |
| Flux simule en continu | Source serveur progressive, pause/reprise et API recevant une mesure par requete. | Parite des decisions avec les 64 journees V3 connues ; memoire conservee entre appels et apres redemarrage. |
| Placement selon un budget | Comparaison exhaustive des 16 sous-ensembles possibles de quatre paires existantes, avec priorites choisies. | Respect du budget et selection selon la couverture ponderee. Illustration retrospective du test V3, sans l'utiliser pour choisir le plan. |
| Regulation et vannes modulantes | Deux vannes TCV, recherche d'une cible d'ouverture puis mouvement limite a 10 points par pas de cinq minutes. | Cinq situations, comparaison avec les vannes ouvertes, gel si donnees absentes ou bilan suspect et controles hydrauliques. |

## Pourquoi deux maquettes

Le reseau initial R-A-B-C-D et ses cinq conduites restent accessibles dans **Reseau initial**, **Inspections** et **Resultats historiques**. Ses codes, modeles et rapports ne sont pas modifies.

La regulation et les interventions utilisent un **laboratoire distinct a deux zones**, alimentees par un tronc commun, avec leurs propres demandes et vannes. Cela permet d'experimenter sans attribuer les anciens scores V3 a un reseau modifie. Les neuf actifs fictifs du classement d'inspection restent ceux de la V1 ; ils ne sont pas remappes artificiellement sur ces deux zones.

## Fichiers de code a lire

| Fichier | Role |
|---|---|
| `aqua_api_v2.py` | API, lancement, source de demonstration et integration des routes V1. |
| `aqua_v2_core.py` | Etat causal, qualite, volumes, journal SQLite et choix des paires. |
| `aqua_v2_hydraulique.py` | Modele des deux zones, simulations d'intervention et controleur des vannes. |
| `aqua_interface_v2.html` | Interface en francais, avec les vues initiales conservees. |
| `aqua_interface_v2.js` | Interactions des nouvelles vues et appels a l'API. |
| `aqua_verifier_v2.py` | Verification locale reproductible, sur une base temporaire. |
| `aqua_producteur_v2.py` | Producteur HTTP optionnel, une mesure simulee par appel. |
| `requirements_api_v2.txt` | Reutilise les dependances API V1 et WNTR 1.5.0. SQLite est inclus dans Python. |
| `AQUA_V2_VERIFICATION.json` | Compte rendu des essais de cette livraison. |

## Prerequis Windows

Conserver le projet complet dans `C:\Users\HP\Documents\aqua-sentinel`, avec `.venv`, les scripts historiques, les donnees V3 et les modeles deja evalues. L'API V1 doit deja fonctionner, comme confirme pendant notre verification.

Le ZIP V2 contient uniquement de nouveaux fichiers V2. Il ne contient ni une copie de nos modeles Linux, ni une nouvelle version des scripts figes de detection ou de risque.

## Installation et lancement

1. Telecharger **AQUA_V2.zip** dans `C:\Users\HP\Downloads`.
2. Copier le bloc complet ci-dessous dans PowerShell.

```powershell
& {
    $ErrorActionPreference = 'Stop'
    Set-Location -LiteralPath 'C:\Users\HP\Documents\aqua-sentinel'

    $aquaV2Archive = Get-ChildItem "$env:USERPROFILE\Downloads" `
        -Filter 'AQUA_V2*.zip' -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    if ($null -eq $aquaV2Archive) {
        throw "Telecharge d'abord AQUA_V2.zip dans Downloads."
    }

    Expand-Archive -LiteralPath $aquaV2Archive.FullName `
        -DestinationPath (Get-Location).Path -Force

    .\.venv\Scripts\python.exe -m pip install -r .\requirements_api_v2.txt
    if ($LASTEXITCODE -ne 0) { throw "Installation V2 interrompue." }

    .\.venv\Scripts\python.exe .\aqua_api_v2.py verifier
    if ($LASTEXITCODE -ne 0) { throw "Verification V2 interrompue." }

    .\.venv\Scripts\python.exe .\aqua_api_v2.py servir --ouvrir
    if ($LASTEXITCODE -ne 0) { throw "Lancement V2 interrompu." }
}
```

La V2 utilise **http://127.0.0.1:8013**, tandis que la V1 conserve le port 8012. Garder PowerShell ouvert ; Ctrl+C arrete la V2. Les mesures deja acceptees restent dans sa base. Au redemarrage, les sessions qui etaient en lecture automatique reprennent ; les sessions en pause restent en pause.

Si le port 8013 est occupe :

```powershell
.\.venv\Scripts\python.exe .\aqua_api_v2.py servir --port 8014 --ouvrir
```

La verification cree seulement une base temporaire et un rapport dans `resultats_aqua\extensions_v2\<identifiant>\verification.json`. Elle ne supprime ni ne reinitialise le suivi de l'interface. La base de suivi par defaut est `resultats_aqua\extensions_v2\aqua_v2.sqlite3` ; des fichiers `-wal` et `-shm` peuvent exister pendant son utilisation.

## Parcours de demonstration

1. **Flux et capteurs** : choisir une journee contenant un signal et creer une session, avec depart proche du premier signal. Recevoir des mesures puis lancer la lecture continue. Changer de page n'arrete pas la source serveur ; utiliser Pause pour l'arreter.
2. Creer une autre session avec **P3 aval absent**, **fige** ou **desynchronise**. Pendant les mesures 11 a 20, examiner la qualite et les intervalles exclus. Le mode fige necessite six valeurs identiques pour signaler une suspicion.
3. **Journal** : ouvrir un incident, planifier une inspection et ajouter une observation. Un changement de statut de suivi ne modifie pas la decision du detecteur.
4. **Regulation** : comparer une hausse de consommation en B, une charge source faible, un capteur absent et une fuite A. Examiner les besoins servis dans chaque zone, pas seulement le chiffre global. Parcourir les 36 pas avec le curseur.
5. **Interventions** : comparer les quatre alternatives. L'isolement du troncon qui fuit peut supprimer la fuite simulee tout en interrompant la desserte de la zone correspondante. Isoler la mauvaise zone peut aggraver la situation ailleurs.
6. **Placement** : essayer 13 mesures, puis 9 ou 7. Modifier une priorite et examiner le plan. Les chiffres du test ancien servent d'illustration retrospective, pas de nouveau test independant.
7. Revenir aux trois vues initiales pour consulter le rejeu V3 et le classement fictif d'inspection.

## Producteur HTTP optionnel

Le bouton Lecture continue utilise une source dans le serveur. Pour montrer une ingestion venant d'un autre processus, ouvrir un second PowerShell pendant que la V2 fonctionne :

```powershell
Set-Location -LiteralPath 'C:\Users\HP\Documents\aqua-sentinel'
.\.venv\Scripts\python.exe .\aqua_producteur_v2.py --nombre 60 --intervalle 0.5
```

Choisir ensuite la nouvelle session dans Flux et capteurs. Ce producteur lit les mesures simulees V3 sauvegardees et les envoie progressivement par HTTP. Ce ne sont pas des capteurs reels. Avec un autre port, ajouter par exemple `--port 8014`.

## Contrats de l'API V2

La documentation interactive est disponible sur `/docs`. Les routes V1 sont conservees.

| Route | Usage |
|---|---|
| `GET /api/v2/sante` | Etat du service et liste des modules. |
| `POST /api/v2/sessions` | Creer une source de rejeu ou une session d'ingestion API. |
| `GET /api/v2/sessions` | Reprendre une session conservee. |
| `POST /api/v2/sessions/{id}/avancer` | Ajouter 1 a 120 mesures de la source de rejeu. |
| `PUT /api/v2/sessions/{id}/lecture` | Demarrer ou suspendre la source serveur. |
| `POST /api/v2/sessions/{id}/mesures` | Recevoir une mesure dans une session de source `api`. |
| `GET /api/v2/sessions/{id}/courbe` | Consulter les 180 derniers points recus ; toutes les ecritures restent dans SQLite. |
| `GET /api/v2/incidents` | Lire les incidents et leur journal. |
| `POST /api/v2/incidents/{id}/notes` | Ajouter un commentaire et un statut de suivi avec cle d'idempotence. |
| `POST /api/v2/regulation` | Calculer une experience de regulation. |
| `POST /api/v2/interventions` | Comparer les interventions pour une fuite definie. |
| `POST /api/v2/capteurs/plan` | Optimiser la couverture selon un budget et des priorites. |
| `GET /api/v2/experiences` | Consulter les references des experiences conservees. |
| `GET /api/v2/experiences/{id}` | Retrouver le resultat complet d'une experience. |
| `GET /api/v2/export` | Exporter les resumes des sessions, incidents et references d'experiences. |

Une ingestion externe utilise `origine="simulation"`, une sequence entiere croissante et une mesure au schema V3 : `scenario_id`, `temps_s` et les huit debits amont/aval en m3/s. Dans cette version de demonstration, une session couvre une journee : `temps_s` va de 0 a 86340, au pas de 60 secondes ; creer une autre session pour une autre journee. Les cinq autres mesures historiques sont optionnelles pour ce detecteur. Une valeur absente ou `null` est signalee comme inexploitable ; elle n'est pas remplacee par zero.

Exemple d'enveloppe, avec les huit debits reels du producteur a mettre dans `mesure` :

```json
{
  "origine": "simulation",
  "sequence": 0,
  "mesure": {"scenario_id": "scenario_declare_a_la_creation", "temps_s": 0},
  "corrections_l_s": {"P2": 0.0, "P3": 0.0, "P4": 0.0, "P5": 0.0},
  "horodatages": {}
}
```

Cet exemple omet volontairement les valeurs de debit : il donnera un etat insuffisant tant qu'elles ne sont pas fournies. La sequence commence a zero dans chaque session ; le scenario_id de la mesure doit correspondre a celui declare lors de sa creation. Le meme numero et le meme contenu donnent la meme decision sans nouvelle ecriture. Le meme numero avec un autre contenu est refuse. Les colonnes de verite de fuite sont refusees.

## Comprendre le modele de regulation

La consigne porte sur une **position d'ouverture simulee**, de 5 a 100 %. Une loi d'exercice `K(u)=2/u^2` relie cette position au coefficient de perte de charge d'une vanne TCV ; elle n'est pas calibree sur une vanne commerciale. Le pourcentage d'ouverture ne signifie pas le meme pourcentage de debit.

Le controleur compare une grille de cibles, puis applique au maximum dix points de pourcentage par pas de cinq minutes. Sa fonction objective est affichee dans les resultats : elle privilegie la zone dont la fraction de besoins servis est la plus faible, puis prend en compte le deficit total, la surpression et les mouvements. Ce choix peut diminuer la desserte d'une zone pour ameliorer celle de l'autre. Les comparaisons montrent les deux.

La pression maximale de 45 m est un objectif penalise, pas une limite physique garantie par le logiciel. Le modele suppose la charge de source et les demandes courantes connues. Le scenario de ressource limitee correspond a une charge hydraulique faible d'un reservoir a charge imposee, pas a l'epuisement d'un volume de reservoir.

Le regulateur suspend ses mouvements quand la mesure B est indisponible ou quand un bilan depasse son seuil de suspicion. Il conserve la consigne precedente ; ce comportement n'equivaut pas a isoler une fuite. L'isolement est compare dans le simulateur d'intervention.

Les calculs sont quasi-statiques. Les transitoires rapides, coups de belier, temps de course reel des actionneurs et retours de position materiels ne sont pas modelises. Le service reste local et ne possede aucun pilote de vanne physique.

## Portee des volumes, de la qualite et du placement

- Le volume estime vient du bilan amont moins aval, corrige du centre de calibration et des soutirages/stockages connus. Il est integre par trapezes sur des intervalles valides ; le dernier point n'est pas extrapole dans le futur.
- Un biais de debitmetre et une fuite equivalente sont indiscernables par ce bilan seul. Le mode Biais est livre pour montrer cette limite ; il peut produire une alerte. Une valeur figee peut aussi correspondre a un vrai regime stable : c'est une suspicion, pas une preuve de panne.
- L'enveloppe de bruit est indicative et n'est pas un intervalle de confiance calibre. Les intervalles invalides sont exclus ; le volume total perdu pendant une panne ne peut pas etre affirme.
- Le placement conserve cinq mesures de base et alloue les autres par paires completes sur P2 a P5. Il optimise exactement cet objectif de couverture ponderee sur seize choix possibles ; il ne recherche pas de nouveaux emplacements geographiques.
- Le rapport de localisation V3 est lu apres le choix du plan pour une illustration descriptive. Aucun nouveau score independant de detection ou de placement n'est annonce.

## Verifications realisees et reste a confirmer

Le rapport livre donne les versions, les empreintes des codes et chaque controle. Les essais de calcul, persistance, HTTP et hydraulique ont ete executes sous Linux/Python 3.12 avec WNTR 1.5.0. Ils seront rejoues sur le poste Windows par la commande `verifier`.

Les interactions des huit vues ont ete verifiees avec jsdom et une API HTTP locale : navigation, lecture/pause, defaut de capteur, annotation comme texte, curseur de regulation, quatre interventions, budget, priorites, reprise apres erreur et producteur HTTP. **Ces tests de DOM ne sont pas un controle visuel dans un vrai navigateur. L'affichage et les interactions sur Windows restent a confirmer apres installation.**

Le classement d'inspection conserve sa loi artificielle et n'annonce aucune casse reelle. Les resultats historiques V3 restent limites a la petite maquette deja evaluee. La V2 n'apporte pas de validation terrain.

## Sources techniques

- US EPA, EPANET 2.2, modele du reseau et vannes : https://usepa.github.io/EPANET2.2/3_network_model.html
- WNTR, simulation hydraulique : https://usepa.github.io/WNTR/hydraulics.html
- WNTR, controles du reseau : https://usepa.github.io/WNTR/controls.html
- WNTR, mesures de resilience et desserte : https://usepa.github.io/WNTR/resilience.html
- Python, sqlite3 : https://docs.python.org/3.13/library/sqlite3.html
- FastAPI, cycle de vie : https://fastapi.tiangolo.com/advanced/events/
- Sources experimentales : les codes figes `aqua_detection_v3.py`, `aqua_risque_v1.py`, leurs rapports conserves et les nouveaux essais de cette livraison.
