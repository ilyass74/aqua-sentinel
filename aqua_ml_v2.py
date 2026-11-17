r"""AQUA Sentinel : ML V2 avec coherence hydraulique de la boucle.

Placer ce fichier a cote de aqua_ml_v1.py, aqua_dataset_v1.py et
aqua_sentinel_demo.py. Aucune nouvelle dependance : scikit-learn 1.9.1.
  .venv\Scripts\python.exe aqua_ml_v2.py entrainer
  .venv\Scripts\python.exe aqua_ml_v2.py evaluer

La V1 reste conservee. La V2 apprend sur les memes train et validation que
la V1, sans relire son ancien test. Les essais de conception de la V2 ont
utilise train/validation ; le bilan de test de la V1 est deja connu.

Nouvelle variable physique : sur la boucle A-B-D-C-A, les differences de
charge s'annulent. En l'absence de soutirage dans les tuyaux, la somme
orientee des pertes de charge calculees a partir des debits est nulle.
Le residu de boucle vaut residu_P2 + residu_P4 - residu_P3 - residu_P5.
Les pressions mesurees s'annulent dans cette combinaison ; elle evite
donc leur bruit dans ce signal particulier. La geometrie, la rugosite et
les sens de mesure restent ceux, connus exactement, de notre maquette.
Il ne s'agit pas d'un bilan de volumes : on compare des pertes de charge.

29 variables causales : residu de boucle signe et absolu, lisses sur
1/3/5/15/30/60 mesures ; residus des 4 tuyaux lisses sur 5/15/30 mesures ;
4 changements relatifs de pression et une variation du debit d'entree.
Les normalisations proviennent seulement des minutes saines de train.
Chaque fuite de train a le meme poids total, uniforme pendant sa duree.
Les identifiants et les temps servent seulement au groupement/tri.
Aucune cible de fuite, ouverture ou duree vraie n'est une variable ML.

Candidats fixes : deux Random Forest et un HistGradientBoosting. Les scores
ne sont pas des probabilites calibrees. Le modele et le seuil sont choisis
sur validation avec la meme regle que la V1 : budget provisoire de 0.2
fausse ouverture par jour, puis priorite aux detections en 5 min, au rappel
des evenements, aux fausses ouvertures et au F1 par minute. Ce budget sur
16 jours de validation n'est pas une garantie statistique. Lissage 3 scores,
3 confirmations d'ouverture et 3 de fermeture sous la moitie du seuil.

Un NOUVEAU test predefini de 64 jours est genere seulement APRES gel du
modele : 32 jours sains et 32 avec fuite unique. Chaque combinaison
P2/P3/P4/P5 x ouverture 4/6/8/12 mm apparait deux fois parmi les fuites.
Graines issues du generateur V1, plan de 320 scenarios, partie test seule,
graine racine 20261016. Aucun des 256 autres scenarios n'est simule.
La V1 figee et la V2 sont comparees sur ces MEMES 64 nouveaux jours.
Le test est lie aux empreintes des deux modeles avant sa generation. Il
ne peut pas etre reutilise comme test inedit pour un autre modele V2.
Les predictions sont sauvegardees avant lecture des etiquettes par
l'evaluateur. Le generateur cree et verifie la verite separement.

Une nouvelle alerte ouverte pendant une fuite compte comme detection.
Une alerte deja ouverte avant la fuite ne compte pas comme detection.
Les fausses ouvertures et les minutes hors fuite sont deux mesures
distinctes ; le lissage peut prolonger l'alerte apres une reparation.

Limites : meme petite maquette et meme famille de simulations. Geometrie
et rugosite exactes, bruit synthetique suppose, absence de defauts capteurs,
de fuites simultanees ou d'ouvrages non modelises. Fuites candidates P2-P5
uniquement ; localisation NON entrainee. Le debut de journee est sain par
construction. Aucune validation terrain ni validation sur un autre reseau.

Sources :
https://usepa.github.io/EPANET2.2/3_network_model.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestClassifier.html
https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingClassifier.html
"""

import argparse
import hashlib
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
from scipy.stats import median_abs_deviation
from sklearn.ensemble import RandomForestClassifier, HistGradientBoostingClassifier
from threadpoolctl import threadpool_limits

import aqua_ml_v1 as v1
import aqua_dataset_v1 as generateur
import aqua_sentinel_demo as initial


DOSSIER = Path(__file__).resolve().parent
GRAINE_TEST = 20261016
NOMBRE_TEST = 64
CONFIGURATIONS = [
    dict(nom="rf8", famille="RandomForest", max_depth=8, min_samples_leaf=32),
    dict(nom="rf14", famille="RandomForest", max_depth=14, min_samples_leaf=8),
    dict(nom="hgb", famille="HistGradientBoosting"),
]


def sources():
    return {Path(p).name: v1.sha256(p) for p in
            [__file__, v1.__file__, generateur.__file__, initial.__file__]}


def verifier_sources(attendu):
    if sources() != attendu:
        raise ValueError("Un code source a change depuis l'entrainement. Conserver les fichiers figes.")


def calibrer(train, y, geometrie):
    v1.calibrer_residus(train, y, geometrie)
    r = v1.residus_hydrauliques(train, geometrie)
    cycle = r.P2 + r.P4 - r.P3 - r.P5
    normaux = cycle[np.asarray(y) == 0]
    geometrie["cycle_centre"] = float(normaux.median())
    geometrie["cycle_echelle"] = max(1e-6, float(median_abs_deviation(normaux, scale="normal")))


def variables(mesures, geometrie):
    """Entrees triees par scenario/temps ; aucun acces a la verite."""
    blocs = []
    for _, groupe in mesures.groupby("scenario_id", sort=False):
        groupe = groupe.reset_index(drop=True)
        residus = v1.residus_hydrauliques(groupe, geometrie)
        cycle = (residus.P2 + residus.P4 - residus.P3 - residus.P5
                 - geometrie["cycle_centre"]) / geometrie["cycle_echelle"]
        x = pd.DataFrame(index=groupe.index)
        for fenetre in [1, 3, 5, 15, 30, 60]:
            moyenne = cycle.rolling(fenetre, min_periods=1, center=False).mean()
            x[f"cycle_moy{fenetre}"] = moyenne
            x[f"cycle_abs_moy{fenetre}"] = moyenne.abs()
        for tuyau in residus:
            param = geometrie["normalisation"][tuyau]
            z = (residus[tuyau] - param["centre"]) / param["echelle"]
            for fenetre in [5, 15, 30]:
                x[f"{tuyau}_moy{fenetre}"] = z.rolling(fenetre, min_periods=1, center=False).mean()
        for noeud in "ABCD":
            pression = groupe[f"pression_{noeud}_m"]
            reference = pression.shift(5).rolling(30, min_periods=1, center=False).mean().fillna(pression.iloc[0])
            x[f"baisse_{noeud}"] = reference - pression.rolling(3, min_periods=1, center=False).mean()
        x["variation_P1"] = groupe.debit_P1_m3_s.diff().fillna(0) * 1000
        blocs.append(x)
    sortie = pd.concat(blocs, ignore_index=True)
    if len(sortie) != len(mesures) or len(sortie.columns) != 29 or not np.isfinite(sortie.to_numpy()).all():
        raise ValueError("Variables V2 invalides.")
    return sortie


def poids_evenements(mesures, y):
    poids = np.ones(len(y), dtype=float)
    groupes = list(mesures.groupby("scenario_id", sort=False).indices.values())
    nombre = sum(np.asarray(y)[indices].any() for indices in groupes)
    if not nombre:
        raise ValueError("Train ne contient aucune fuite.")
    masse = int(np.count_nonzero(y)) / nombre
    for indices in groupes:
        positifs = indices[np.asarray(y)[indices] == 1]
        if len(positifs):
            poids[positifs] = masse / len(positifs)
    return poids


def creer_estimateur(configuration):
    if configuration["famille"] == "RandomForest":
        return RandomForestClassifier(
            n_estimators=160, max_depth=configuration["max_depth"],
            min_samples_leaf=configuration["min_samples_leaf"],
            class_weight="balanced_subsample", max_features="sqrt",
            random_state=20261016, n_jobs=2)
    return HistGradientBoostingClassifier(
        max_iter=160, max_leaf_nodes=15, min_samples_leaf=32, learning_rate=0.08,
        l2_regularization=1, early_stopping=False, class_weight="balanced",
        random_state=20261016)


def scores(modele, x):
    with threadpool_limits(limits=2):
        return modele.predict_proba(x)[:, list(modele.classes_).index(1)]


def entrainer(modele_v1):
    modele_v1 = modele_v1.resolve()
    if not (modele_v1 / "_ENTRAINE").is_file():
        raise ValueError("Modele V1 incomplet.")
    meta_v1 = json.loads((modele_v1 / "entrainement.json").read_text(encoding="utf-8"))
    v1.verifier_empreinte(v1.__file__, meta_v1["sha256_code"])
    v1.verifier_empreinte(initial.__file__, meta_v1["sha256_reseau_initial"])
    v1.verifier_empreinte(modele_v1 / "modele.joblib", meta_v1["sha256_modele"])
    if meta_v1["sklearn"] != sklearn.__version__:
        raise ValueError("Utiliser la meme version de scikit-learn que pour la V1.")
    dataset = Path(meta_v1["dataset"])
    if not (dataset / "_SUCCESS").is_file():
        raise ValueError("Jeu d'apprentissage de la V1 absent ou incomplet.")
    audit = meta_v1["audit_dataset"]
    print("V2 : lecture de train et validation uniquement. Ancien test non lu.", flush=True)
    train = v1.lire_mesures(dataset, "train", audit)
    validation = v1.lire_mesures(dataset, "validation", audit)
    if set(train.scenario_id) & set(validation.scenario_id):
        raise ValueError("Scenario commun entre train et validation.")
    y_train = v1.lire_etiquettes(dataset, "train", audit, train)
    y_validation = v1.lire_etiquettes(dataset, "validation", audit, validation)
    if set(y_train) != {0, 1}:
        raise ValueError("Train doit contenir les deux classes.")
    geometrie, _ = v1.geometrie_fixe()
    calibrer(train, y_train, geometrie)
    x_train, x_validation = variables(train, geometrie), variables(validation, geometrie)
    poids = poids_evenements(train, y_train)
    meilleur = None
    candidats, configurations = [], []
    print(f"Apprentissage : {len(train)} mesures, {len(x_train.columns)} variables.", flush=True)
    for configuration in CONFIGURATIONS:
        print("Apprentissage et validation : " + configuration["nom"], flush=True)
        modele = creer_estimateur(configuration)
        with threadpool_limits(limits=2):
            modele.fit(x_train, y_train, sample_weight=poids)
        score = scores(modele, x_validation)
        choix, seuils = v1.choisir_seuil(validation, score, y_validation)
        configurations.append(dict(choix, configuration=configuration))
        candidats.extend(dict(m, configuration=configuration["nom"]) for m in seuils)
        if meilleur is None or v1.cle_selection(choix) > v1.cle_selection(meilleur[0]):
            meilleur = choix, modele, score, configuration
    choix, modele, score, configuration = meilleur
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = DOSSIER / "models" / "aqua_ml_v2" / identifiant
    run.mkdir(parents=True)
    bundle = {"modele": modele, "geometrie": geometrie, "variables": list(x_train.columns),
              "seuil": choix["seuil"], "version_sklearn": sklearn.__version__}
    joblib.dump(bundle, run / "modele.joblib", compress=3)
    pd.DataFrame(candidats).to_csv(run / "seuils_validation.csv", index=False)
    prediction = v1.predictions(validation, score, choix["seuil"])
    prediction.to_csv(run / "predictions_validation.csv", index=False)
    _, evenements, scenarios = v1.evaluer_alertes(prediction, y_validation)
    pd.DataFrame(evenements).to_csv(run / "evenements_validation.csv", index=False)
    pd.DataFrame(scenarios).to_csv(run / "scenarios_validation.csv", index=False)
    v1.ecrire_json(run / "metriques_validation.json", choix)
    v1.ecrire_json(run / "entrainement.json", {
        "origine": "simulation", "dataset": str(dataset.resolve()), "audit_dataset": audit,
        "modele_v1": str(modele_v1), "sha256_modele_v1": meta_v1["sha256_modele"],
        "sha256_meta_v1": v1.sha256(modele_v1 / "entrainement.json"),
        "sha256_modele_v2": v1.sha256(run / "modele.joblib"), "sources_sha256": sources(),
        "python": platform.python_version(), "sklearn": sklearn.__version__,
        "numpy": np.__version__, "pandas": pd.__version__, "wntr": wntr.__version__,
        "configuration": configuration, "configurations_validation": configurations,
        "ponderation": "poids total positif egal par fuite ; poids uniforme pendant chaque fuite",
        "seuil": choix["seuil"], "variables": list(x_train.columns),
        "ids_train": sorted(set(train.scenario_id)), "ids_validation": sorted(set(validation.scenario_id)),
        "budget_fa_validation_jour": v1.BUDGET_FA_JOUR,
        "nouveau_test": {"seed": GRAINE_TEST, "nombre": NOMBRE_TEST,
                         "plan_generateur": "planifier(320, 20261016), partie test seulement"},
        "nouveau_test_utilise_pour_reglage": False,
        "ancien_test_v1": "deja connu ; non utilise comme test final V2",
        "limite": "Meme maquette et meme famille de simulations ; aucune validation terrain.",
    })
    (run / "_ENTRAINE").write_text("PASS technique\n", encoding="utf-8")
    v1.afficher("VALIDATION V2 (sert au choix du modele et du seuil)", choix)
    print(f"Configuration retenue : {configuration['nom']} ; seuil fige : {choix['seuil']:.2f}")
    print(f"Modele V2 : {run}")
    return run


def verifier_gel(run, meta):
    verifier_sources(meta["sources_sha256"])
    v1.verifier_empreinte(run / "modele.joblib", meta["sha256_modele_v2"])
    baseline = Path(meta["modele_v1"])
    v1.verifier_empreinte(baseline / "modele.joblib", meta["sha256_modele_v1"])
    v1.verifier_empreinte(baseline / "entrainement.json", meta["sha256_meta_v1"])
    if sklearn.__version__ != meta["sklearn"]:
        raise ValueError("La version scikit-learn differe de celle de l'entrainement.")
    if meta["nouveau_test"]["seed"] != GRAINE_TEST or meta["nouveau_test"]["nombre"] != NOMBRE_TEST:
        raise ValueError("Le protocole du nouveau test a change.")


def generer_test(run, meta):
    """Generer seulement apres gel ; aucune prediction ni fit dans ce code."""
    dossier = DOSSIER / "data" / "aqua_test_ml_v2" / f"seed{GRAINE_TEST}_n{NOMBRE_TEST}"
    dossier.mkdir(parents=True, exist_ok=True)
    liaison = {"sha256_modele_v1": meta["sha256_modele_v1"],
               "sha256_modele_v2": meta["sha256_modele_v2"],
               "sources_sha256": meta["sources_sha256"]}
    verrou = dossier / "reservation.json"
    if verrou.is_file():
        if json.loads(verrou.read_text(encoding="utf-8")) != liaison:
            raise ValueError("Ce nouveau test est deja reserve a d'autres modeles. Il ne serait plus un test inedit.")
    else:
        if any(dossier.iterdir()):
            raise ValueError("Dossier de test existant sans reservation verifiable.")
        v1.ecrire_json(verrou, liaison)
    if (dossier / "_SUCCESS").is_file():
        return dossier, json.loads((dossier / "resume_dataset.json").read_text(encoding="utf-8"))
    plan = [cas for cas in generateur.planifier(NOMBRE_TEST * 5, GRAINE_TEST) if cas["partie"] == "test"]
    if len(plan) != NOMBRE_TEST or sum(c["avec_fuite"] for c in plan) != NOMBRE_TEST // 2:
        raise ValueError("Plan de nouveau test incorrect.")
    # Tous les anciens IDs, y compris l'ancien test deja connu, sont exclus.
    ancien_protocole = json.loads((Path(meta["dataset"]) / "protocole.json").read_text(encoding="utf-8"))
    anciens = ancien_protocole["scenarios"]
    if ({c["scenario_id"] for c in plan} & {c["scenario_id"] for c in anciens}
            or {c["seed"] for c in plan} & {c["seed"] for c in anciens}):
        raise ValueError("Un scenario ou une graine a deja ete utilise.")
    comptes = {}
    for cas in plan:
        if cas["avec_fuite"]:
            cle = (cas["tuyau_cible"], cas["diametre_fuite_mm"])
            comptes[cle] = comptes.get(cle, 0) + 1
    if len(comptes) != 16 or set(comptes.values()) != {2}:
        raise ValueError("Combinaisons tuyau/ouverture non equilibrees.")
    (dossier / "capteurs").mkdir(exist_ok=True)
    (dossier / "verite").mkdir(exist_ok=True)
    v1.ecrire_json(dossier / "protocole.json", {
        "origine": "simulation", "seed": GRAINE_TEST, "scenarios": plan,
        "reservation": liaison, "comparaison": "V1 et V2 figees avant ces simulations",
        "ids_ou_graines_communs_avec_ancien_dataset": 0,
        "limite": "Nouveaux scenarios de la meme famille sur la meme maquette.",
    })
    mesures, verites = [], []
    erreur_bilan = 0.0
    for numero, cas in enumerate(plan, 1):
        print(f"[{numero}/{len(plan)}] Nouveau test : {cas['scenario_id']}", flush=True)
        x, y, erreur = generateur.simuler(cas)
        if len(x) != v1.LIGNES_JOUR or len(y) != v1.LIGNES_JOUR:
            raise ValueError("Scenario de nouveau test incomplet.")
        actif = ((y.temps_s >= cas["debut_fuite_s"]) & (y.temps_s < cas["fin_fuite_s"])) if cas["avec_fuite"] else np.zeros(len(y), dtype=bool)
        if not np.array_equal(y.fuite_active.to_numpy(dtype=bool), actif):
            raise ValueError("Calendrier physique de fuite incorrect.")
        mesures.append(x)
        verites.append(y)
        erreur_bilan = max(erreur_bilan, float(erreur))
    for nom, blocs in [("capteurs", mesures), ("verite", verites)]:
        table = pd.concat(blocs, ignore_index=True)
        destination = dossier / nom / "test.csv"
        temporaire = destination.with_suffix(".csv.tmp")
        table.to_csv(temporaire, index=False)
        temporaire.replace(destination)
    audit = {"origine": "simulation", "verifications": "PASS", "scenarios_total": NOMBRE_TEST,
             "scenarios_communs_avec_ancien_dataset": 0, "labels_dans_fichiers_capteurs": False,
             "erreur_bilan_max_m3_s": erreur_bilan, "jeux": {"test": {
                 "scenarios": NOMBRE_TEST, "scenarios_avec_fuite": NOMBRE_TEST // 2,
                 "lignes": NOMBRE_TEST * v1.LIGNES_JOUR,
                 "sha256_capteurs": v1.sha256(dossier / "capteurs" / "test.csv"),
                 "sha256_verite": v1.sha256(dossier / "verite" / "test.csv")}}}
    # Audit technique du generateur, sans apprentissage ni choix de seuil.
    x = v1.lire_mesures(dossier, "test", audit)
    v1.lire_etiquettes(dossier, "test", audit, x)
    if len(set(x.scenario_id)) != NOMBRE_TEST or erreur_bilan > 1e-6:
        raise ValueError("Audit technique du nouveau test en echec.")
    v1.ecrire_json(dossier / "resume_dataset.json", audit)
    (dossier / "_SUCCESS").write_text("PASS technique\n", encoding="utf-8")
    return dossier, audit


def tracer_comparaison(resultats, chemin):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 2, figsize=(11, 7))
    noms = ["V1", "V2"]
    couleurs = ["#64748b", "#087f8c"]
    spec = [
        ("fuites_detectees", "Fuites detectees (sur 32)", NOMBRE_TEST // 2, ".0f"),
        ("fuites_detectees_en_5min", "Fuites detectees en 5 minutes (sur 32)", NOMBRE_TEST // 2, ".0f"),
        ("fausses_alertes_par_jour", "Fausses ouvertures par jour simule", None, ".3f"),
        ("f1_minutes", "F1 par minute (couverture des fuites et fausses minutes)", 1, ".3f"),
    ]
    for ax, (cle, titre, limite, fmt) in zip(axes.flat, spec):
        valeurs = [resultats[nom][cle] for nom in noms]
        barres = ax.bar(noms, valeurs, color=couleurs, width=0.5)
        ax.bar_label(barres, labels=[format(x, fmt) for x in valeurs], padding=4)
        ax.set_title(titre, fontsize=10)
        ax.set_ylim(0, (limite * 1.15) if limite else max(0.3, max(valeurs) * 1.25))
        if cle == "fausses_alertes_par_jour":
            ax.axhline(v1.BUDGET_FA_JOUR, color="#b45309", ls="--", lw=1, label="Budget provisoire de validation")
            ax.legend(fontsize=8)
        ax.grid(axis="y", alpha=0.18)
        ax.set_axisbelow(True)
    fig.suptitle("AQUA Sentinel | V1 et V2 sur les memes 64 nouveaux jours simules")
    fig.text(0.5, 0.015, "Modeles figes avant ce test. Meme maquette synthetique ; aucune validation terrain.", ha="center", fontsize=9)
    fig.tight_layout(rect=[0, 0.04, 1, 0.96])
    fig.savefig(chemin, dpi=150)
    plt.close(fig)


def evaluer(run):
    run = run.resolve()
    if not (run / "_ENTRAINE").is_file():
        raise ValueError("Modele V2 incomplet.")
    meta = json.loads((run / "entrainement.json").read_text(encoding="utf-8"))
    verifier_gel(run, meta)
    if (run / "_TEST_EVALUE").is_file():
        resultats = json.loads((run / "comparaison_test.json").read_text(encoding="utf-8"))
        for nom in ["V1", "V2"]:
            v1.afficher("NOUVEAU TEST DEJA EVALUE : " + nom, resultats[nom])
        print(f"Resultats conserves : {run}")
        return resultats
    dossier, audit = generer_test(run, meta)
    mesures = v1.lire_mesures(dossier, "test", audit)
    if set(mesures.scenario_id) & (set(meta["ids_train"]) | set(meta["ids_validation"])):
        raise ValueError("Scenario commun entre apprentissage et nouveau test.")
    baseline = joblib.load(Path(meta["modele_v1"]) / "modele.joblib")
    nouveau = joblib.load(run / "modele.joblib")
    predictions = {}
    for nom, bundle, calcul in [("V1", baseline, v1.variables), ("V2", nouveau, variables)]:
        x = calcul(mesures, bundle["geometrie"])
        if list(x.columns) != bundle["variables"]:
            raise ValueError("Variables incoherentes : " + nom)
        prediction = v1.predictions(mesures, scores(bundle["modele"], x), bundle["seuil"])
        prediction.to_csv(run / f"predictions_test_{nom.lower()}.csv", index=False)
        predictions[nom] = prediction
    # L'evaluateur lit les etiquettes uniquement apres les deux predictions.
    y = v1.lire_etiquettes(dossier, "test", audit, mesures)
    resultats = {}
    for nom, prediction in predictions.items():
        metriques, evenements, scenarios = v1.evaluer_alertes(prediction, y)
        metriques.update({"origine": "simulation", "seuil": (baseline if nom == "V1" else nouveau)["seuil"],
                          "nouveau_test_utilise_pour_reglage": False,
                          "limite": "Nouveaux scenarios, meme maquette et meme famille ; aucune validation terrain."})
        resultats[nom] = metriques
        v1.ecrire_json(run / f"metriques_test_{nom.lower()}.json", metriques)
        pd.DataFrame(evenements).to_csv(run / f"evenements_test_{nom.lower()}.csv", index=False)
        pd.DataFrame(scenarios).to_csv(run / f"scenarios_test_{nom.lower()}.csv", index=False)
    v1.ecrire_json(run / "comparaison_test.json", resultats)
    pd.DataFrame([dict(modele=nom, **m) for nom, m in resultats.items()]).to_csv(run / "comparaison_test.csv", index=False)
    tracer_comparaison(resultats, run / "comparaison_test.png")
    v1.tracer_test(predictions["V2"], y, run / "bilan_test_v2.png")
    v1.ecrire_json(run / "audit_test.json", {"dataset_test": str(dossier), "audit_dataset": audit,
        "sha256_predictions_v1": v1.sha256(run / "predictions_test_v1.csv"),
        "sha256_predictions_v2": v1.sha256(run / "predictions_test_v2.csv"),
        "predictions_sauvegardees_avant_lecture_etiquettes_par_evaluateur": True,
        "sources_sha256": sources()})
    verifier_gel(run, meta)
    (run / "_TEST_EVALUE").write_text("PASS technique\n", encoding="utf-8")
    for nom in ["V1", "V2"]:
        v1.afficher("NOUVEAU TEST INDEPENDANT : " + nom, resultats[nom])
    print("PASS technique : execution verifiee. La qualite reste celle des scores.")
    print(f"Resultats : {run}")
    return resultats


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["entrainer", "evaluer"])
    parser.add_argument("--modele-v1", type=Path, help="Dossier du modele V1 de reference")
    parser.add_argument("--modele", type=Path, help="Dossier du modele V2 a evaluer")
    args = parser.parse_args()
    if args.action == "entrainer":
        modele_v1 = args.modele_v1 or v1.dernier_dossier(DOSSIER / "models" / "aqua_ml_v1", "_ENTRAINE")
        entrainer(modele_v1)
    else:
        modele_v2 = args.modele or v1.dernier_dossier(DOSSIER / "models" / "aqua_ml_v2", "_ENTRAINE")
        evaluer(modele_v2)


if __name__ == "__main__":
    main()
