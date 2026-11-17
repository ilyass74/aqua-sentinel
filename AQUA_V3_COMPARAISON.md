# AQUA Sentinel - comparaison du document de vision et du code

Comparaison du PDF fourni, **AQUA_Sentinel_Vision_Plateforme(1).pdf**, 13 pages, avec le projet V1/V2 effectivement present. Livraison : plateforme V3. Le numero V3 de la plateforme est distinct du detecteur historique V3 **Bilans_13**.

## Lecture de la comparaison

« Ajoute » signifie code et parcours de maquette executes dans cette livraison. Cela ne signifie ni capteur reel installe, ni validation sur un reseau d'eau, ni garantie industrielle. Les exemples numeriques du PDF ne sont pas des mesures de notre projet.

| Fonction du PDF | Avant cette livraison | Resultat de la plateforme V3 | Limite explicite |
|---|---|---|---|
| Six blocs Monitor, Detect, Locate, Intelligence, Control, Operations (p. 3) | Modules repartis entre scripts et huit vues V2. | Couche de supervision et API V3, avec acces aux anciennes vues et modules. | Architecture de maquette locale, pas une infrastructure IoT de production. |
| Debits et pressions (p. 3-4) | Treize mesures simulees, huit debits appaires utilises par le detecteur. | Conserves ; analyses par secteur et preuves ajoutees. | Aucune nouvelle mesure terrain. |
| Acoustique, niveau et etat des equipements (p. 3) | Non fournis dans le dataset. | Contrat d'ingestion complementaire, niveau et etat declare de pompe ; absence affichee par defaut. | Le statut declare d'une pompe ne cree pas une pompe dans le modele hydraulique. |
| Detection persistante et petites fuites (p. 4) | Bilans_13 avec persistance, teste historiquement sur la maquette. | Detecteur inchange ; les autres signaux expliquent l'alerte. | Les nouveaux ajouts ne beneficient pas automatiquement du score historique 32/32. |
| Fusion des preuves debit / pression / acoustique / contexte / qualite (p. 4) | Pas de presentation commune des preuves. | Indice explicatif et details de chaque contribution, avec couverture des signaux disponibles. | Poids d'exercice, non calibres comme probabilite. La persistance represente le contexte temporel ; une evidence absente n'est pas fabriquee. |
| Score de confiance et gravite (p. 4) | Alarme physique et niveau de seuil. | Indice de preuves distinct de la gravite et de la qualite des mesures. | « 89/100 » ne devient ni « 89 % de probabilite » ni automatiquement « critique ». |
| Causes probables et recommandation (p. 2, 7) | Bilan suspect, avec limite fuite/biais. | Preuves, premier signal recu, pression, volumes et conseil d'inspection par secteur. | Fuite, biais et soutirage inconnu restent des hypotheses concurrentes. |
| Localisation par secteur et intervalle (p. 4) | Conduite candidate P2, P3, P4 ou P5 entre deux compteurs. | Carte interactive, selection, filtres et fiche de chaque secteur pilote. | Pas de coordonnee precise d'un trou. Les secteurs ne sont pas des DMA de terrain certifiees. |
| Localisation acoustique avancee (p. 4) | Absente. | Experience conditionnelle sur deux signaux synchronises ; correlation, retard et position estimee si pic exploitable. Rejet des signaux constants ou ambigus. | Necessite distance et vitesse d'onde declarees. Aucun signal acoustique reel disponible ; la vitesse, les horloges, le filtrage et les reflexions demandent une calibration. Ne remplace pas la localisation hydraulique figee. |
| Baseline journaliere (p. 5) | Reference de calibration des bilans uniquement. | Profils debit/pression par quart d'heure, mediane et dispersion sur les seuls scenarios sains de train. | Reference explicative distincte du detecteur ; aucun ajustement sur le test. |
| Baseline hebdomadaire (p. 5) | Pas de dates dans les scenarios. | Import de references datees avec fuseau, regroupement par secteur, jour et heure, version et provenance. | Descriptif du lot fourni ; les classes absentes restent inconnues. Aucun jour de semaine n'est attribue artificiellement aux anciens scenarios. |
| Minimum Night Flow (p. 5) | Absent. | Minimum sur les points nocturnes recus et alerte apres cinq points consecutifs au-dessus de la reference + trois dispersions. | Fenetre 01:00-05:00 de la simulation. Une nuit partielle est indiquee. Une consommation legitime peut expliquer le signal. |
| Statistiques et priorites par secteur (p. 5) | Volumes par paire et incidents disponibles dans V2. | Pressions minimale/maximale recues, taux de mesures valides en alerte, volumes, classement des secteurs et interventions rattachees. | Sur la session selectionnee, pas sur une journee civile reelle. |
| Vannes modulantes et equilibrage (p. 6) | Laboratoire WNTR a deux zones et regulateur borne. | Conserves ; cycle de controle avec evaluation deterministe et comparaison avant/apres. | Loi de vanne d'exercice ; aucun gain de terrain garanti. |
| Safety Engine distinct de l'intelligence (p. 7) | Gel du regulateur sur qualite ou bilan suspect ; maximum de pression penalise. | Moteur separe : fraicheur, communication, mode, qualite, pressions prevues, limites de mouvement, contexte versionne et approbation. | Verifie les etats quasi-statiques du modele ; pas une certification ni une simulation de coup de belier. |
| Pressions minimale et maximale (p. 7) | Objectif de surpression souple dans V2. | Refus d'une nouvelle action si une zone alimentee sort des limites d'exercice 10-45 mCE dans les etats prevus. | Si la ressource rend ces limites impossibles, l'action est refusee ; aucune pression reelle n'est « garantie » par ce logiciel. |
| Zone critique (p. 7) | Pas de politique explicite. | Zone B critique dans le laboratoire ; isolement assiste avec autorisation explicite et justification. Isolement automatique refuse. | Affectation de demonstration, pas inventaire d'etablissements sensibles reels. |
| Perte de communication (p. 7) | Pas de contrat de commande autonome. | Refus de nouveaux mouvements ; maintien de la consigne precedente et trace de la decision. | Ne garantit pas la position d'un actionneur physique qui n'est pas connecte. |
| Emergency Isolation (p. 7) | Comparaison de quatre alternatives dont isolement aux deux extremites. | Conserves ; propositions d'isolement A/B, vannes d'extremites nommees, effets sur pression, fuite et service, autorisation et verification. | Vannes d'isolement binaires ideales. Leurs transitoires et temps de course ne sont pas modelises. |
| Usagers affectes (p. 7) | Pas de population connue. | Effectifs facultatifs declares par zone ; total des zones presque entierement privees de service. | Les effectifs manquants restent inconnus ; pas d'inference du nombre d'usagers a partir d'un simple pourcentage de debit. |
| Digital Twin (p. 8) | Modele original et laboratoire hydraulique a deux zones. | Conserves ; toute proposition est simulee avant execution dans le laboratoire. | Les deux reseaux restent distincts ; aucune remap automatique de P2 en zone A. Le laboratoire ne contient pas tous les types d'equipements d'un reseau industriel. |
| Verification post-action (p. 3, 8) | Comparaison avant/apres des simulations uniquement. | Etat d'attente de verification ; nouveau retour simule, comparaison des pressions et diagnostic de retour conforme, bloque, absent ou trop lent. Ecart = gel. | Retour genere par le meme modele hydraulique ; pas une validation independante d'un actionneur. |
| Agent IA source (p. 8) | Absent. | Assistant local a regles pour les questions du PDF, simulation de fermeture en lecture seule, sources et limites. Option Groq pour selection de faits, avec rejet des identifiants inconnus. Aucun droit de commande. | Le mode local n'est pas un LLM. Le service distant exige une configuration volontaire et n'a pas ete appele dans les essais. Les textes et nombres sont assembles par le serveur. |
| Maintenance predictive (p. 9) | Classement d'inspection sur actifs et loi de casse fictifs. | Conserve et accessible a l'assistant avec son origine fictive. | Aucune probabilite de rupture reelle, aucun « 78 % / 6 mois » invente. Des historiques de terrain sont indispensables pour cette validation. |
| Sensor Health (p. 9) | Absence, valeur figee suspecte, decalage horaire, plage et trou temporel. | Conserves ; tendance du residu et divergence entre compteurs, indice d'utilisabilite et signaux acoustiques absents visibles. | Une tendance hydraulique peut ressembler a une derive ; pas de diagnostic certain de capteur. |
| Valve Health (p. 9) | Pas de verification d'actionneur. | Retours simules bloque, trop lent ou indisponible, avec gel du controle. | Aucun vrai retour de position ni temps de course mesure. |
| Dashboard principal (p. 10) | Vue reseau et vues fonctionnelles distinctes. | Huit indicateurs calcules, carte coloree, fiche de secteur et conseil. | Pas de nombre demonstratif du PDF repris comme mesure ; valeurs absentes affichees comme inconnues. |
| Filtres carte et tendances (p. 10) | Selection d'une conduite et courbes. | Filtres alertes, pression, nocturne, maintenance, capteurs ; clavier, courbe et details. | Schema du reseau connu, pas une carte geographique importee. |
| Maintenance et interventions (p. 3, 10) | Commentaires et statut de suivi des signaux. | Ordres d'intervention, responsable, echeance, transitions, historique et preuves avant/apres. Cloture exige trois nouvelles mesures utilisables sans alerte. | Le retour du rejeu ne prouve pas une reparation physique. Aucun message envoye au responsable. |
| KPI, audit et rapports (p. 3, 10) | JSON de resultats et journal SQLite V2. | JSON, CSV et rapport HTML imprimable en PDF ; journal chaine par hachages et controle d'integrite. | Chaine locale sans ancrage externe ; pas une preuve inviolable face a un administrateur. |

## Les fonctions qui exigent encore des donnees ou du materiel

Le code de maquette ne peut pas remplacer les etapes suivantes : installer et calibrer les capteurs, confirmer de vraies limites de pression, caracteriser les actionneurs et leur position de repli, disposer d'un reseau inventorie et d'historiques de casses, puis evaluer sur des observations independantes. Ces points ne sont pas declares accomplis.

Les nouveaux scores explicatifs, seuils nocturnes et regles de sante ont ete testes techniquement sur des cas controles. Leur precision et leur rappel en conditions reelles ne sont pas connus. Le resultat historique Bilans_13 reste limite a son protocole initial.

## Sources verifiables

- Document fourni : **AQUA_Sentinel_Vision_Plateforme(1).pdf**, pages 2-11 ; les pages 12-13 precisent le positionnement et le caractere propose des exemples.
- Sources du projet precedemment livre : `aqua_api_v1.py`, `aqua_api_v2.py`, `aqua_v2_core.py`, `aqua_v2_hydraulique.py`, `aqua_detection_v3.py`, `aqua_risque_v1.py` et rapports figes locaux.
- Nouveaux codes : `aqua_api_v3.py`, `aqua_v3_monitor.py`, `aqua_v3_ops.py`, `aqua_v3_acoustique.py`, `aqua_v3_agent.py`, interface et verificateur inclus dans cette livraison.
- US EPA, WNTR, simulation hydraulique : https://usepa.github.io/WNTR/hydraulics.html
- US EPA, EPANET 2.2, modele reseau et vannes : https://usepa.github.io/EPANET2.2/3_network_model.html
- Almeida et al., Sensors 2014, etude experimentale de correlation acoustique : https://pmc.ncbi.nlm.nih.gov/articles/PMC4004009/
- Groq, compatibilite et sorties structurees : https://console.groq.com/docs/openai et https://console.groq.com/docs/structured-outputs
- Python, SQLite : https://docs.python.org/3.13/library/sqlite3.html

Les sources externes soutiennent les methodes et interfaces generales. Elles ne valident ni les seuils choisis pour cette maquette ni les performances de notre implementation.
