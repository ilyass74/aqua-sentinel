r"""AQUA Sentinel : gel de Bilans_13 et nouveau test de detection/localisation.

  .venv\Scripts\python.exe aqua_detection_v3.py figer
  .venv\Scripts\python.exe aqua_detection_v3.py evaluer

Placer a cote des scripts existants. Memes dependances. La configuration
Bilans_13 est choisie sur l'etude de validation : quatre bilans amont/aval,
treize mesures disponibles, dont huit debits utilises par cette regle.
La V2 ML a neuf capteurs reste un comparateur FIGE. Ce changement ajoute
des mesures et une regle physique ; il ne reentraine pas un modele ML.

figer : copie les centres/dispersions/seuils calibres sur les fenetres
saines de train dans models/aqua_detection_v3/<id>/modele.json. Tous les
codes, la calibration et le ML comparateur sont lies par empreintes SHA256.
Le choix et les reglages precedent toute simulation du NOUVEAU test.

evaluer : genere 64 nouveaux jours (32 sains, 32 avec fuite unique), plan
du generateur V1 de 320 cas, partie test seule, graine FIXE 20261019.
Chaque combinaison P2/P3/P4/P5 x ouverture 4/6/8/12 mm apparait deux fois.
Les IDs et graines doivent etre disjoints de l'ancien dataset et du test
ML V2 deja connu. Aucun autre cas du plan de 320 n'est simule.
Les deux predictions sont ecrites avant lecture des etiquettes par
l'evaluateur. Le generateur cree/audite la verite physique separement.
Un verrou reserve ce test aux deux detecteurs et aux codes figes ; il ne
peut pas servir de test inedit a une version ulterieure modifiee.

Mesures : les neuf capteurs historiques (bruit graine [seed,2]) et quatre
debits aval extraits DIRECTEMENT des sorties du solveur, chacun dans tous
les cas, sains ou avec fuite (bruit independant, graine [seed,3]). Aucune
mesure n'est fabriquee en soustrayant le debit vrai de fuite. Le bruit de
debit suppose reste sigma 0.05 L/s, sans biais ni derive ni decalage.

Prediction : moyenne causale de trois bilans signes q_amont-q_aval,
centrage appris sur train, seuil fixe quatre dispersions robustes, puis
meme operateur que l'etude (moyenne trois scores ; trois confirmations
pour ouvrir, trois sous le demi-seuil pour fermer). L'alerte globale est
l'OU des quatre alertes. Les deux premieres mesures sont neutres.
Les scores normalises ne sont PAS des probabilites.

Localisation physique : parmi les paires actuellement en alerte, signaler
le tuyau au score lisse/seuil maximal au moment de la decision. Evaluation
au PREMIER declenchement pendant chaque vraie fuite ; les fuites manquees
comptent comme echecs dans le score global. Aucune localisation ML n'est
entrainee. La zone localisee est le tuyau candidat entre ses deux points
de mesure, pas une position precise le long du tuyau.

Limites essentielles : petite maquette connue, fuite unique entre paires,
aucun soutirage legitime ni stockage entre les deux debitmetres. Un
soutirage legitime entre eux serait confondu avec une perte par ce seul
bilan. Synchronisation et signes de debit coherents requis. Le test reste
de la meme famille synthetique, avec debut de journee sain ; aucune
validation terrain, sur un autre reseau ou face aux pannes de capteurs.

Sources :
https://usepa.github.io/EPANET2.2/12_analysis_algorithms.html
https://usepa.github.io/WNTR/units.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
"""

import argparse
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
import sklearn
import wntr

import aqua_ml_v1 as v1
import aqua_ml_v2 as v2
import aqua_dataset_v1 as generateur
import aqua_etude_capteurs_v1 as etude


DOSSIER = Path(__file__).resolve().parent
GRAINE_TEST = 20261019
NOMBRE_TEST = 64
CONFIGURATION = "Bilans_13"
MESURES = v1.CAPTEURS + etude.EXTRA


def sources():
    return dict(etude.sources(), **{Path(__file__).name: v1.sha256(__file__)})


def verifier_modele(modele):
    if modele["configuration"] != CONFIGURATION or modele["fenetre_bilans"] != 3 or etude.FENETRE != 3:
        raise ValueError("Configuration de bilan differente du protocole fige.")
    if set(modele["calibration"]) != set(etude.TUYAUX):
        raise ValueError("Calibration incomplete.")
    for p, c in modele["calibration"].items():
        if not all(np.isfinite(c[k]) for k in ["centre_l_s", "dispersion_l_s", "seuil_l_s", "facteur"]):
            raise ValueError("Calibration non finie : " + p)
        if c["dispersion_l_s"] <= 0 or c["facteur"] != 4.0 or not np.isclose(c["seuil_l_s"], 4 * c["dispersion_l_s"]):
            raise ValueError("Seuil de bilan incompatible : " + p)


def predire(mesures, modele):
    """Capteurs uniquement, present/passe. Accepte aussi un prefixe de journee."""
    verifier_modele(modele)
    necessaires = [f"debit_{p}{suffixe}_m3_s" for p in etude.TUYAUX for suffixe in ["", "_aval"]]
    if not set(v1.CLES + necessaires).issubset(mesures.columns):
        raise ValueError("Debits amont/aval ou cles absents.")
    if not set(mesures.columns).issubset(v1.CLES + MESURES):
        raise ValueError("Colonne non capteur ou information de verite dans les entrees.")
    mesures = mesures.sort_values(v1.CLES).reset_index(drop=True)
    if mesures[v1.CLES].isna().any().any() or mesures.duplicated(v1.CLES).any() or not np.isfinite(mesures[necessaires].to_numpy()).all():
        raise ValueError("Debits ou cles invalides.")
    for _, g in mesures.groupby("scenario_id", sort=False):
        if len(g) > 1 and not np.equal(np.diff(g.temps_s.to_numpy()), v1.PAS).all():
            raise ValueError("Historique de debit non continu au pas de 60 s.")
    bilans, complet = etude.bilans_lisses(mesures)
    alertes, niveaux = pd.DataFrame(index=mesures.index), pd.DataFrame(index=mesures.index)
    for p in etude.TUYAUX:
        c = modele["calibration"][p]
        score = np.where(complet, bilans[p].to_numpy() - c["centre_l_s"], 0.0)
        resultat = v1.predictions(mesures, score, c["seuil_l_s"])
        alertes[p] = resultat.alerte.to_numpy()
        niveaux[p] = resultat.score_lisse.to_numpy() / c["seuil_l_s"]
    actif = alertes.any(axis=1).to_numpy()
    choix = np.argmax(np.where(alertes.to_numpy(dtype=bool), niveaux.to_numpy(), -np.inf), axis=1)
    prediction = mesures[v1.CLES].copy()
    prediction["alerte"] = actif.astype(int)
    prediction["tuyau_signale"] = np.where(actif, np.asarray(etude.TUYAUX)[choix], "")
    for p in etude.TUYAUX:
        prediction[f"alerte_{p}"] = alertes[p]
        prediction[f"niveau_{p}"] = niveaux[p]
    return prediction


def figer(dossier_etude):
    dossier_etude = dossier_etude.resolve()
    if not (dossier_etude / "_SUCCESS").is_file():
        raise ValueError("Etude de capteurs incomplete.")
    resume = json.loads((dossier_etude / "resume_etude.json").read_text(encoding="utf-8"))
    if resume["sources_sha256"] != etude.sources():
        raise ValueError("Les sources de l'etude ont change.")
    calibration = json.loads((dossier_etude / "calibration_bilans.json").read_text(encoding="utf-8"))
    if calibration != resume["calibration_bilans_train"]:
        raise ValueError("Calibration differente du bilan de l'etude.")
    modele_v2 = Path(resume["modele_v2"])
    meta_v2 = json.loads((modele_v2 / "entrainement.json").read_text(encoding="utf-8"))
    v2.verifier_gel(modele_v2, meta_v2)
    modele = {"version": 3, "configuration": CONFIGURATION, "methode": "bilans physiques par paire, pas un modele ML",
              "fenetre_bilans": 3, "pas_s": 60, "calibration": calibration,
              "capteurs_disponibles": MESURES, "capteurs_installes": 13,
              "capteurs_utilises": [f"debit_{p}{s}_m3_s" for p in etude.TUYAUX for s in ["", "_aval"]],
              "sens_references": {"P2": "A vers B", "P3": "A vers C", "P4": "B vers D", "P5": "C vers D"},
              "localisation": "argmax du niveau parmi les paires en alerte, au temps de decision",
              "hypothese": "aucun soutirage legitime ni stockage entre les points de chaque paire"}
    verifier_modele(modele)
    identite = {"sources_sha256": sources(), "sha256_resume_etude": v1.sha256(dossier_etude / "resume_etude.json"),
                "sha256_ml_v2": meta_v2["sha256_modele_v2"], "nouveau_test": {"seed": GRAINE_TEST, "scenarios": NOMBRE_TEST}}
    racine = DOSSIER / "models" / "aqua_detection_v3"
    # Idempotence : le meme gel retrouve le modele deja cree, meme apres test.
    if racine.exists():
        for existant in sorted(racine.iterdir(), reverse=True):
            if (existant / "_GELE").is_file():
                ancien = json.loads((existant / "gel.json").read_text(encoding="utf-8"))
                if ancien.get("identite") == identite:
                    verifier_gel(existant, ancien)
                    print(f"Configuration deja figee : {existant}")
                    return existant
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = racine / identifiant
    run.mkdir(parents=True)
    v1.ecrire_json(run / "modele.json", modele)
    v1.ecrire_json(run / "gel.json", {"identite": identite, "etude": str(dossier_etude),
        "dataset_original": resume["dataset_original"], "modele_ml_v2": str(modele_v2),
        "sha256_modele": v1.sha256(run / "modele.json"), "sklearn": sklearn.__version__,
        "python": platform.python_version(), "wntr": wntr.__version__,
        "validation_ayant_servi_au_choix": resume["resultats_validation"][CONFIGURATION],
        "test_utilise_pour_reglage": False, "choix": "Bilans_13, retenu avant le nouveau test"})
    (run / "_GELE").write_text("PASS technique\n", encoding="utf-8")
    print("Configuration figee : Bilans_13, 13 mesures disponibles, 8 debits utilises.")
    print(f"Modele : {run}")
    return run


def verifier_gel(run, meta):
    if sources() != meta["identite"]["sources_sha256"]:
        raise ValueError("Un code source a change depuis le gel.")
    v1.verifier_empreinte(run / "modele.json", meta["sha256_modele"])
    v1.verifier_empreinte(Path(meta["modele_ml_v2"]) / "modele.joblib", meta["identite"]["sha256_ml_v2"])
    if sklearn.__version__ != meta["sklearn"]:
        raise ValueError("Version scikit-learn differente de celle du comparateur fige.")
    if meta["identite"]["nouveau_test"] != {"seed": GRAINE_TEST, "scenarios": NOMBRE_TEST}:
        raise ValueError("Protocole du nouveau test modifie.")


def simuler_nouveau(cas):
    """Mesures de tous les capteurs ; verite creee pour l'audit/evaluation."""
    reseau = generateur.construire_reseau(cas)
    resultat = wntr.sim.WNTRSimulator(reseau).run_sim()
    calendrier = np.arange(0, generateur.DUREE + 1, generateur.PAS)
    if not np.array_equal(resultat.link["flowrate"].index, calendrier):
        raise ValueError("Nouveau scenario incomplet.")
    n = generateur.LIGNES
    mesures = pd.DataFrame({"scenario_id": cas["scenario_id"], "temps_s": calendrier[:n]})
    bruit = np.random.default_rng(np.random.SeedSequence([cas["seed"], 2]))
    for colonne in v1.CAPTEURS:
        if colonne.startswith("pression_"):
            propre = resultat.node["pressure"][colonne.split("_")[1]].iloc[:n].to_numpy()
            sigma = generateur.BRUIT_PRESSION_M
        else:
            propre = resultat.link["flowrate"][colonne.split("_")[1]].iloc[:n].to_numpy()
            sigma = generateur.BRUIT_DEBIT_M3_S
        mesures[colonne] = propre + bruit.normal(0, sigma, n)
    bruit_aval = np.random.default_rng(np.random.SeedSequence([cas["seed"], 3]))
    erreur = 0.0
    for p in etude.TUYAUX:
        aval = resultat.link["flowrate"][p + "_aval"].iloc[:n].to_numpy()
        mesures[f"debit_{p}_aval_m3_s"] = aval + bruit_aval.normal(0, generateur.BRUIT_DEBIT_M3_S, n)
        ecart = resultat.link["flowrate"][p] - resultat.link["flowrate"][p + "_aval"] - resultat.node["leak_demand"]["L" + p[1:]]
        erreur = max(erreur, float(ecart.abs().max()))
    fuite = resultat.node["leak_demand"][["L2", "L3", "L4", "L5"]].sum(axis=1)
    demande = resultat.node["demand"][generateur.h.NOEUDS_MESURES].sum(axis=1)
    erreur = max(erreur, float((resultat.link["flowrate"].P1 - demande - fuite).abs().max()))
    actif = ((calendrier >= cas["debut_fuite_s"]) & (calendrier < cas["fin_fuite_s"])) if cas["avec_fuite"] else np.zeros(len(calendrier), dtype=bool)
    if (not np.isfinite(mesures[MESURES].to_numpy()).all() or not np.isfinite(erreur) or erreur > 1e-6
            or not np.allclose(fuite[~actif], 0, atol=1e-10) or not (fuite[actif] > 0).all()):
        raise ValueError("Audit physique du nouveau scenario en echec.")
    verite = pd.DataFrame({"scenario_id": cas["scenario_id"], "temps_s": calendrier[:n],
        "fuite_active": actif[:n].astype(int), "tuyau_cible": np.where(actif[:n], cas["tuyau_cible"] or "", ""),
        "debit_fuite_m3_s": fuite.iloc[:n].to_numpy()})
    return mesures[v1.CLES + MESURES], verite, erreur


def preparer_test(run, meta):
    dossier = DOSSIER / "data" / "aqua_test_detection_v3" / f"seed{GRAINE_TEST}_n{NOMBRE_TEST}"
    dossier.mkdir(parents=True, exist_ok=True)
    reservation = {"modele_bilans_sha256": meta["sha256_modele"], "identite": meta["identite"]}
    verrou = dossier / "reservation.json"
    if verrou.is_file():
        if json.loads(verrou.read_text(encoding="utf-8")) != reservation:
            raise ValueError("Ce test est reserve a d'autres codes/modeles. Il ne serait plus inedit.")
    else:
        if any(dossier.iterdir()):
            raise ValueError("Dossier de test sans reservation verifiable.")
        v1.ecrire_json(verrou, reservation)
    if (dossier / "_SUCCESS").is_file():
        return dossier, json.loads((dossier / "audit.json").read_text(encoding="utf-8"))
    plan = [c for c in generateur.planifier(NOMBRE_TEST * 5, GRAINE_TEST) if c["partie"] == "test"]
    compte = {}
    for c in plan:
        if c["avec_fuite"]:
            cle = (c["tuyau_cible"], c["diametre_fuite_mm"])
            compte[cle] = compte.get(cle, 0) + 1
    if len(plan) != NOMBRE_TEST or len(compte) != 16 or set(compte.values()) != {2}:
        raise ValueError("Plan du nouveau test incorrect.")
    ancien = json.loads((Path(meta["dataset_original"]) / "protocole.json").read_text(encoding="utf-8"))["scenarios"]
    audit_ml = Path(meta["modele_ml_v2"]) / "audit_test.json"
    if not audit_ml.is_file():
        raise ValueError("Audit du test ML precedent absent ; separation non verifiable.")
    ancien_test = Path(json.loads(audit_ml.read_text(encoding="utf-8"))["dataset_test"]) / "protocole.json"
    if not ancien_test.is_file():
        raise ValueError("Protocole du test ML precedent absent ; separation non verifiable.")
    ancien += json.loads(ancien_test.read_text(encoding="utf-8"))["scenarios"]
    if ({c["scenario_id"] for c in plan} & {c["scenario_id"] for c in ancien}
            or {c["seed"] for c in plan} & {c["seed"] for c in ancien}):
        raise ValueError("ID ou graine partage avec un ancien scenario.")
    (dossier / "capteurs").mkdir(exist_ok=True)
    (dossier / "verite").mkdir(exist_ok=True)
    v1.ecrire_json(dossier / "protocole.json", {"seed": GRAINE_TEST, "scenarios": plan,
        "reservation": reservation, "scenarios_ou_graines_communs_avec_anciens": 0,
        "limite": "Nouveaux scenarios de la meme famille ; aucune validation terrain."})
    capteurs, verites = [], []
    erreur_max = 0.0
    for i, cas in enumerate(plan, 1):
        print(f"[{i}/{len(plan)}] Test neuf Bilans_13 : {cas['scenario_id']}", flush=True)
        x, y, erreur = simuler_nouveau(cas)
        capteurs.append(x)
        verites.append(y)
        erreur_max = max(erreur_max, erreur)
    empreintes = {}
    for nom, tables in [("capteurs", capteurs), ("verite", verites)]:
        table = pd.concat(tables, ignore_index=True).sort_values(v1.CLES).reset_index(drop=True)
        cible = dossier / nom / "test.csv"
        temporaire = cible.with_suffix(".csv.tmp")
        table.to_csv(temporaire, index=False)
        temporaire.replace(cible)
        empreintes[nom] = v1.sha256(cible)
    audit = {"sha256": empreintes, "scenarios": NOMBRE_TEST, "scenarios_avec_fuite": NOMBRE_TEST // 2,
             "lignes": NOMBRE_TEST * generateur.LIGNES, "erreur_bilan_max_m3_s": erreur_max,
             "labels_dans_capteurs": False, "capteurs_installes": 13}
    # Audit du generateur, distinct de l'evaluateur ; aucun reglage ici.
    verifier_capteurs(dossier, audit)
    verite = pd.read_csv(dossier / "verite" / "test.csv")
    if int(verite.fuite_active.groupby(verite.scenario_id).any().sum()) != NOMBRE_TEST // 2:
        raise ValueError("Nombre de jours avec fuite incorrect.")
    v1.ecrire_json(dossier / "audit.json", audit)
    (dossier / "_SUCCESS").write_text("PASS technique\n", encoding="utf-8")
    return dossier, audit


def verifier_capteurs(dossier, audit):
    chemin = dossier / "capteurs" / "test.csv"
    v1.verifier_empreinte(chemin, audit["sha256"]["capteurs"])
    x = pd.read_csv(chemin).sort_values(v1.CLES).reset_index(drop=True)
    if list(x.columns) != v1.CLES + MESURES or len(x) != audit["lignes"] or x.duplicated(v1.CLES).any():
        raise ValueError("Schema ou nombre de lignes de test incorrect.")
    if not np.isfinite(x[MESURES].to_numpy()).all() or x[v1.CLES].isna().any().any():
        raise ValueError("Capteurs de test non finis ou cle manquante.")
    if x.scenario_id.nunique() != audit["scenarios"]:
        raise ValueError("Nombre de scenarios de test incorrect.")
    for _, groupe in x.groupby("scenario_id", sort=False):
        if not np.array_equal(groupe.temps_s, np.arange(generateur.LIGNES) * 60):
            raise ValueError("Calendrier de test incomplet.")
    return x


def evaluer_localisation(prediction, evenements, verite):
    lignes = []
    for evenement in evenements:
        sid, debut = evenement["scenario_id"], evenement["debut_fuite_s"]
        cible = verite.loc[verite.scenario_id.eq(sid) & verite.temps_s.eq(debut), "tuyau_cible"].iloc[0]
        signale = None
        if evenement["detectee"]:
            temps = debut + evenement["delai_s"]
            signale = str(prediction.loc[prediction.scenario_id.eq(sid) & prediction.temps_s.eq(temps), "tuyau_signale"].iloc[0])
        lignes.append(dict(evenement, tuyau_cible=str(cible), tuyau_signale_premiere_alerte=signale,
                           correctement_localisee=signale == cible))
    correctes = sum(e["correctement_localisee"] for e in lignes)
    detectees = sum(e["detectee"] for e in lignes)
    rapides = sum(e["correctement_localisee"] and e["detectee_en_5min"] for e in lignes)
    return {"evenements": len(lignes), "correctement_localises": correctes,
            "localises_parmi_detectes": correctes / detectees if detectees else None,
            "localises_sur_toutes_fuites": correctes / len(lignes) if lignes else None,
            "detectes_et_localises_en_5min": rapides,
            "methode": "bilan physique par paire au premier declenchement pendant la fuite ; pas une localisation ML"}, lignes


def tracer(resultats, chemin):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    noms = ["ML_9", CONFIGURATION]
    spec = [("fuites_detectees", "Fuites detectees sur 32", 36),
            ("fuites_detectees_en_5min", "Detectees en cinq minutes sur 32", 36),
            ("fausses_alertes", "Faux declenchements sur 64 jours", None),
            ("f1_minutes", "F1 par minute", 1.15)]
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    for ax, (cle, titre, limite) in zip(axes.flat, spec):
        valeurs = [resultats[n][cle] for n in noms]
        barres = ax.bar(noms, valeurs, color=["#64748b", "#087f8c"], width=.5)
        ax.bar_label(barres, labels=[f"{v:.3f}" if cle == "f1_minutes" else str(v) for v in valeurs], padding=3)
        ax.set_ylim(0, limite if limite else max(1, max(valeurs) * 1.3))
        ax.set_title(titre, fontsize=10)
        ax.grid(axis="y", alpha=.2)
        ax.set_axisbelow(True)
    fig.suptitle("AQUA Sentinel | Detection figee, nouveau test synthetique")
    fig.text(.5, .015, "Bilans_13 dispose de quatre debitmetres supplementaires. Aucune validation terrain.", ha="center", fontsize=9)
    fig.tight_layout(rect=[0, .04, 1, .96])
    fig.savefig(chemin, dpi=150)
    plt.close(fig)


def evaluer(run):
    run = run.resolve()
    if not (run / "_GELE").is_file():
        raise ValueError("Figer le modele avant le nouveau test.")
    meta = json.loads((run / "gel.json").read_text(encoding="utf-8"))
    verifier_gel(run, meta)
    if (run / "_TEST_EVALUE").is_file():
        bilan = json.loads((run / "bilan_test.json").read_text(encoding="utf-8"))
        for nom, metriques in bilan["detection"].items():
            v1.afficher("TEST DEJA EVALUE : " + nom, metriques)
        print("Localisation physique :", bilan["localisation"])
        print(f"Resultats conserves : {run}")
        return bilan
    dossier, audit = preparer_test(run, meta)
    mesures = verifier_capteurs(dossier, audit)
    modele = json.loads((run / "modele.json").read_text(encoding="utf-8"))
    prediction = predire(mesures, modele)
    prediction.to_csv(run / "predictions_bilans13_test.csv", index=False)
    ml = joblib.load(Path(meta["modele_ml_v2"]) / "modele.joblib")
    x_ml = v2.variables(mesures[v1.CLES + v1.CAPTEURS], ml["geometrie"])
    if list(x_ml.columns) != ml["variables"]:
        raise ValueError("Variables du comparateur ML incompatibles.")
    prediction_ml = v1.predictions(mesures[v1.CLES + v1.CAPTEURS], v2.scores(ml["modele"], x_ml), ml["seuil"])
    prediction_ml.to_csv(run / "predictions_ml9_test.csv", index=False)
    # Premier acces aux etiquettes par l'evaluateur : les deux predictions sont ecrites.
    v1.verifier_empreinte(dossier / "verite" / "test.csv", audit["sha256"]["verite"])
    verite = pd.read_csv(dossier / "verite" / "test.csv", keep_default_na=False).sort_values(v1.CLES).reset_index(drop=True)
    if not verite[v1.CLES].equals(mesures[v1.CLES]) or not verite.fuite_active.isin([0, 1]).all():
        raise ValueError("Verite de test mal alignee ou non binaire.")
    resultats = {}
    evenements_bilans = None
    for nom, p in [("ML_9", prediction_ml), (CONFIGURATION, prediction)]:
        m, evenements, scenarios = v1.evaluer_alertes(p, verite.fuite_active.to_numpy())
        resultats[nom] = dict(m, origine="simulation", test_utilise_pour_reglage=False)
        pd.DataFrame(evenements).to_csv(run / ("evenements_" + nom + "_test.csv"), index=False)
        pd.DataFrame(scenarios).to_csv(run / ("scenarios_" + nom + "_test.csv"), index=False)
        if nom == CONFIGURATION:
            evenements_bilans = evenements
    localisation, details = evaluer_localisation(prediction, evenements_bilans, verite)
    pd.DataFrame(details).to_csv(run / "localisation_test.csv", index=False)
    comparaison = pd.DataFrame([dict(configuration=nom, **m) for nom, m in resultats.items()])
    comparaison.to_csv(run / "comparaison_test.csv", index=False)
    bilan = {"origine": "simulation", "configuration_principale": CONFIGURATION,
             "detection": resultats, "localisation": localisation,
             "dataset_test": str(dossier), "audit_dataset": audit,
             "sources_sha256": sources(), "test_utilise_pour_reglage": False,
             "predictions_ecrites_avant_lecture_etiquettes_par_evaluateur": True,
             "sha256_predictions": {"bilans13": v1.sha256(run / "predictions_bilans13_test.csv"),
                                    "ml9": v1.sha256(run / "predictions_ml9_test.csv")},
             "limite": "Meme maquette et famille ; capteurs supplementaires sans biais/derive/panne/decalage ; aucun soutirage entre paires ; aucune validation terrain."}
    v1.ecrire_json(run / "bilan_test.json", bilan)
    tracer(resultats, run / "bilan_test.png")
    verifier_gel(run, meta)
    (run / "_TEST_EVALUE").write_text("PASS technique\n", encoding="utf-8")
    for nom, m in resultats.items():
        v1.afficher("NOUVEAU TEST FIGE : " + nom, m)
    print("\nLOCALISATION PHYSIQUE AU PREMIER DECLENCHEMENT")
    print(f"Fuites correctement localisees : {localisation['correctement_localises']}/{localisation['evenements']}")
    print(f"Detectees et localisees en 5 min : {localisation['detectes_et_localises_en_5min']}/{localisation['evenements']}")
    print("PASS technique : execution et gel verifies ; consulter les scores et limites.")
    print(f"Resultats : {run}")
    return bilan


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["figer", "evaluer"])
    parser.add_argument("--etude", type=Path, help="Dossier de l'etude de capteurs terminee")
    parser.add_argument("--modele", type=Path, help="Dossier de la detection V3 figee")
    args = parser.parse_args()
    if args.action == "figer":
        figer(args.etude or v1.dernier_dossier(DOSSIER / "resultats_aqua" / "etude_capteurs_v1", "_SUCCESS"))
    else:
        evaluer(args.modele or v1.dernier_dossier(DOSSIER / "models" / "aqua_detection_v3", "_GELE"))


if __name__ == "__main__":
    main()
