"""AQUA Sentinel : premier modele ML de detection de fuite.

Installation : .venv\\Scripts\\python.exe -m pip install scikit-learn==1.9.1
Execution :
  .venv\\Scripts\\python.exe aqua_ml_v1.py entrainer
  .venv\\Scripts\\python.exe aqua_ml_v1.py evaluer

Le modele apprend sur train uniquement. Le seuil d'alerte est choisi sur
validation avec une regle fixee ci-dessous. Le modele et le seuil sont
sauvegardes avant lecture des capteurs ou etiquettes de test. L'evaluation
de test enregistre d'abord les predictions, puis lit les etiquettes.

Entrees : 4 pressions et 5 debits. Les pressions servent a calculer des residus
hydrauliques approximatifs avec la geometrie fixe. Les variables du modele
sont ces residus, les changements du debit d'entree, les baisses de pression
relatives au passe et leurs statistiques causales de 5/15 mesures.
Les niveaux absolus de debit ne sont pas utilises. La reference de pression
est la moyenne des 30 observations precedant les 5 dernieres observations ;
au demarrage, la premiere mesure sert de reference. Les scenarios generes
commencent par au moins 30 minutes sans fuite ; ce demarrage ne represente
pas le cas d'une fuite deja presente lors de l'installation des capteurs.
Les residus sont centres et mis a l'echelle a partir des seules minutes
normales de train. Les pressions absolues ne sont pas des variables du modele.
Aucune variable issue des parametres de fuite ou de sa verite n'est fournie
au modele. Les identifiants servent a grouper les observations et les temps
a les ordonner ; ils ne sont pas des variables explicatives.

Trois configurations Random Forest fixees ci-dessous sont comparees sur
validation. Chaque evenement de fuite de train recoit le meme poids total
positif, quelle que soit sa duree. La moitie de ce poids porte sur ses cinq
premieres minutes et l'autre moitie sur la suite, afin de privilegier une
detection rapide. Le choix de modele et de seuil sur validation
privilegie, parmi les seuils respectant 0.2 fausse alerte par jour simule,
le nombre de fuites detectees en 5 minutes, puis le nombre total detecte,
puis moins de fausses alertes, puis le F1 par minute. Un seuil 1.01 permet
le cas limite sans alerte si aucun seuil utile ne respecte le budget.
Le budget est un choix provisoire, pas une garantie statistique.

Les scores du Random Forest ne sont pas des probabilites calibrees. Une
moyenne causale sur 3 scores precede l'alerte : 3 confirmations a l'ouverture,
3 sous la moitie du seuil a la fermeture. Les decisions ne sont pas antidatees.
Une fausse alerte est un episode ouvrant hors d'une fuite. Les minutes en
alerte hors fuite, y compris le delai de fermeture, sont comptees separement.
Une alerte ouverte avant une fuite ne compte pas comme sa detection.

Portee : detection binaire sur une seule maquette synthetique ; localisation
non entrainee ici. Peu d'evenements de test ; aucune validation terrain.
Sources :
https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.RandomForestClassifier.html
https://scikit-learn.org/stable/modules/classification_threshold.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
https://usepa.github.io/EPANET2.2/3_network_model.html
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
from sklearn.ensemble import RandomForestClassifier
from scipy.stats import median_abs_deviation


DOSSIER = Path(__file__).resolve().parent
CAPTEURS = ([f"pression_{n}_m" for n in "ABCD"]
            + [f"debit_P{i}_m3_s" for i in range(1, 6)])
CLES = ["scenario_id", "temps_s"]
PAS = 60
LIGNES_JOUR = 1440
BUDGET_FA_JOUR = 0.2
SEUILS = [float(x) for x in np.round(np.arange(0.05, 1.0, 0.05), 2)] + [1.01]
RF_PARAMETRES = dict(n_estimators=160, max_depth=14, min_samples_leaf=8,
                     max_features="sqrt", class_weight="balanced_subsample",
                     random_state=20261015, n_jobs=2)
CONFIGURATIONS = [
    dict(nom="arbres_courts", max_depth=5, min_samples_leaf=64),
    dict(nom="arbres_moyens", max_depth=8, min_samples_leaf=32),
    dict(nom="arbres_profonds", max_depth=14, min_samples_leaf=8),
]


def sha256(chemin):
    hachage = hashlib.sha256()
    with Path(chemin).open("rb") as fichier:
        for bloc in iter(lambda: fichier.read(1024 * 1024), b""):
            hachage.update(bloc)
    return hachage.hexdigest()


def ecrire_json(chemin, valeur):
    chemin.write_text(json.dumps(valeur, indent=2, allow_nan=False), encoding="utf-8")


def dernier_dossier(racine, marqueur):
    candidats = sorted(x for x in racine.iterdir() if x.is_dir() and (x / marqueur).is_file()) if racine.exists() else []
    if not candidats:
        raise ValueError(f"Aucun dossier complet avec {marqueur} dans {racine}.")
    return candidats[-1]


def verifier_empreinte(chemin, attendu):
    if sha256(chemin) != attendu:
        raise ValueError(f"Fichier modifie ou incomplet : {chemin}")


def lire_mesures(dataset, partie, audit):
    chemin = dataset / "capteurs" / (partie + ".csv")
    verifier_empreinte(chemin, audit["jeux"][partie]["sha256_capteurs"])
    table = pd.read_csv(chemin)
    if list(table.columns) != CLES + CAPTEURS:
        raise ValueError("Colonnes capteurs incorrectes ou etiquettes presentes.")
    if table[CLES].isna().any().any() or table.duplicated(CLES).any():
        raise ValueError("Cle capteur absente ou dupliquee.")
    if not np.isfinite(table[CAPTEURS].to_numpy()).all():
        raise ValueError("Mesure absente ou non finie.")
    table = table.sort_values(CLES).reset_index(drop=True)
    for identifiant, groupe in table.groupby("scenario_id", sort=False):
        if not np.array_equal(groupe.temps_s, np.arange(LIGNES_JOUR) * PAS):
            raise ValueError("Calendrier incomplet : " + identifiant)
    if len(table) != audit["jeux"][partie]["lignes"]:
        raise ValueError("Nombre de mesures incorrect.")
    return table


def lire_etiquettes(dataset, partie, audit, mesures):
    chemin = dataset / "verite" / (partie + ".csv")
    verifier_empreinte(chemin, audit["jeux"][partie]["sha256_verite"])
    table = pd.read_csv(chemin, usecols=CLES + ["fuite_active"])
    table = table.sort_values(CLES).reset_index(drop=True)
    if not table[CLES].equals(mesures[CLES]) or table.duplicated(CLES).any():
        raise ValueError("Etiquettes et capteurs mal alignes.")
    if not table.fuite_active.isin([0, 1]).all():
        raise ValueError("Etiquette binaire incorrecte.")
    return table.fuite_active.to_numpy(dtype=int)


def geometrie_fixe():
    import aqua_sentinel_demo as initial
    reseau = initial.creer_reseau(False)
    geometrie = {"elevations": {n: float(reseau.get_node(n).elevation) for n in "ABCD"}, "tuyaux": {}}
    for nom, debut, fin in [("P2", "A", "B"), ("P3", "A", "C"), ("P4", "B", "D"), ("P5", "C", "D")]:
        lien = reseau.get_link(nom)
        longueur = lien.length
        if nom + "_aval" in reseau.link_name_list:
            longueur += reseau.get_link(nom + "_aval").length
        # Constante SI employee par WNTR 1.5.0 ; formule H-W approchee
        # aux tres faibles debits, ou le solveur utilise une regularisation.
        coefficient = 10.666829500036352 * longueur / (lien.roughness ** 1.852 * lien.diameter ** 4.871)
        geometrie["tuyaux"][nom] = {"debut": debut, "fin": fin, "coefficient_hw": coefficient}
    return geometrie, sha256(initial.__file__)


def residus_hydrauliques(mesures, geometrie):
    resultat = pd.DataFrame(index=mesures.index)
    for nom, asset in geometrie["tuyaux"].items():
        a, b = asset["debut"], asset["fin"]
        delta_h = ((mesures[f"pression_{a}_m"] + geometrie["elevations"][a])
                   - (mesures[f"pression_{b}_m"] + geometrie["elevations"][b]))
        q = mesures[f"debit_{nom}_m3_s"]
        resultat[nom] = delta_h - asset["coefficient_hw"] * q * q.abs() ** 0.852
    return resultat


def calibrer_residus(train, y_train, geometrie):
    normaux = residus_hydrauliques(train, geometrie).loc[np.asarray(y_train) == 0]
    geometrie["normalisation"] = {
        nom: {"centre": float(normaux[nom].median()),
              "echelle": max(1e-3, float(median_abs_deviation(normaux[nom], scale="normal")))}
        for nom in geometrie["tuyaux"]
    }


def variables(mesures, geometrie):
    """Calculer chaque historique separement, avec le present et le passe."""
    blocs = []
    for _, groupe in mesures.groupby("scenario_id", sort=False):
        groupe = groupe.reset_index(drop=True)
        x = pd.DataFrame(index=groupe.index)
        x["variation_P1_l_s"] = groupe["debit_P1_m3_s"].diff().fillna(0) * 1000
        for noeud in "ABCD":
            pression = groupe[f"pression_{noeud}_m"]
            reference_passee = pression.shift(5).rolling(30, min_periods=1, center=False).mean().fillna(pression.iloc[0])
            recente = pression.rolling(3, min_periods=1, center=False).mean()
            x[f"baisse_relative_{noeud}_m"] = reference_passee - recente
        residus = residus_hydrauliques(groupe, geometrie)
        z_noms = []
        for nom in residus:
            param = geometrie.get("normalisation", {}).get(nom, {"centre": 0.0, "echelle": 1.0})
            colonne = f"residu_norm_{nom}"
            x[colonne] = (residus[nom] - param["centre"]) / param["echelle"]
            z_noms.append(colonne)
        moyenne5 = x.rolling(5, min_periods=1, center=False).mean().add_suffix("_moy5")
        moyenne15 = x.rolling(15, min_periods=1, center=False).mean().add_suffix("_moy15")
        dispersion = x.rolling(5, min_periods=1, center=False).std(ddof=0).add_suffix("_std5")
        variation = x.diff(5).fillna(0).add_suffix("_delta5")
        complement = pd.DataFrame({
            "ecart_negatif_max_moy5": -moyenne5[[n + "_moy5" for n in z_noms]].min(axis=1),
            "ecart_negatif_max_moy15": -moyenne15[[n + "_moy15" for n in z_noms]].min(axis=1),
            "somme_ecarts_negatifs_moy5": (-moyenne5[[n + "_moy5" for n in z_noms]]).clip(lower=0).sum(axis=1),
        })
        blocs.append(pd.concat([x, moyenne5, moyenne15, dispersion, variation, complement], axis=1))
    resultat = pd.concat(blocs, ignore_index=True)
    if len(resultat) != len(mesures) or not np.isfinite(resultat.to_numpy()).all():
        raise ValueError("Variables ML incompletes ou non finies.")
    return resultat


def declencher(scores, seuil):
    alerte = False
    ouverture = fermeture = 0
    sortie = []
    for score in scores:
        if alerte:
            fermeture = fermeture + 1 if score < seuil / 2 else 0
            if fermeture >= 3:
                alerte = False
                ouverture = fermeture = 0
        else:
            ouverture = ouverture + 1 if score >= seuil else 0
            if ouverture >= 3:
                alerte = True
                ouverture = fermeture = 0
        sortie.append(int(alerte))
    return sortie


def predictions(mesures, scores, seuil):
    table = mesures[CLES].copy()
    table["score_fuite"] = np.asarray(scores)
    blocs = []
    for _, groupe in table.groupby("scenario_id", sort=False):
        groupe = groupe.copy()
        groupe["score_lisse"] = groupe.score_fuite.rolling(3, min_periods=1, center=False).mean()
        groupe["alerte"] = declencher(groupe.score_lisse, seuil)
        blocs.append(groupe)
    return pd.concat(blocs, ignore_index=True)


def evaluer_alertes(prediction, y):
    predit = prediction.alerte.to_numpy(dtype=bool)
    vrai = np.asarray(y, dtype=bool)
    if len(vrai) != len(predit):
        raise ValueError("Predictions et etiquettes de tailles differentes.")
    vp = int(np.count_nonzero(predit & vrai))
    fp = int(np.count_nonzero(predit & ~vrai))
    fn = int(np.count_nonzero(~predit & vrai))
    vn = int(np.count_nonzero(~predit & ~vrai))
    precision = vp / (vp + fp) if vp + fp else None
    rappel = vp / (vp + fn) if vp + fn else None
    f1 = 2 * vp / (2 * vp + fp + fn) if 2 * vp + fp + fn else 0.0
    table = prediction.copy()
    table["vrai"] = vrai
    evenements, scenarios = [], []
    for identifiant, groupe in table.groupby("scenario_id", sort=False):
        a = groupe.alerte.to_numpy(dtype=bool)
        v = groupe.vrai.to_numpy(dtype=bool)
        temps = groupe.temps_s.to_numpy()
        debuts_alertes = np.flatnonzero(a & ~np.r_[False, a[:-1]])
        debuts_fuites = np.flatnonzero(v & ~np.r_[False, v[:-1]])
        fins_fuites = np.flatnonzero(v & ~np.r_[v[1:], False]) + 1
        fausses = int(np.count_nonzero(~v[debuts_alertes]))
        scenarios.append({"scenario_id": identifiant, "avec_fuite": bool(v.any()),
                          "episodes_alerte": len(debuts_alertes), "fausses_alertes": fausses,
                          "minutes_alerte_hors_fuite": int(np.count_nonzero(a & ~v))})
        for debut, fin in zip(debuts_fuites, fins_fuites):
            candidats = debuts_alertes[(debuts_alertes >= debut) & (debuts_alertes < fin)]
            delai = int(temps[candidats[0]] - temps[debut]) if len(candidats) else None
            evenements.append({"scenario_id": identifiant, "debut_fuite_s": int(temps[debut]),
                               "fin_fuite_s": int(temps[fin - 1] + PAS),
                               "detectee": delai is not None, "delai_s": delai,
                               "detectee_en_5min": delai is not None and delai <= 300})
    detectees = sum(e["detectee"] for e in evenements)
    rapides = sum(e["detectee_en_5min"] for e in evenements)
    delais = [e["delai_s"] for e in evenements if e["detectee"]]
    jours = len(prediction) * PAS / 86400
    fausses = sum(s["fausses_alertes"] for s in scenarios)
    metriques = {
        "scenarios": len(scenarios), "jours_simules": jours,
        "fuites_total": len(evenements), "fuites_detectees": detectees,
        "rappel_evenements": detectees / len(evenements) if evenements else None,
        "fuites_detectees_en_5min": rapides,
        "rappel_evenements_en_5min": rapides / len(evenements) if evenements else None,
        "fausses_alertes": fausses, "fausses_alertes_par_jour": fausses / jours,
        "cas_sains_avec_alerte": sum(not s["avec_fuite"] and s["episodes_alerte"] > 0 for s in scenarios),
        "minutes_alerte_hors_fuite": fp,
        "precision_minutes": precision, "rappel_minutes": rappel, "f1_minutes": f1,
        "vrais_positifs_minutes": vp, "faux_negatifs_minutes": fn, "vrais_negatifs_minutes": vn,
        "delai_median_s": float(np.median(delais)) if delais else None,
        "delai_max_s": max(delais) if delais else None,
    }
    return metriques, evenements, scenarios


def choisir_seuil(mesures, scores, y):
    candidats = []
    for seuil in SEUILS:
        m, _, _ = evaluer_alertes(predictions(mesures, scores, seuil), y)
        candidats.append(dict(m, seuil=seuil))
    admissibles = [m for m in candidats if m["fausses_alertes_par_jour"] <= BUDGET_FA_JOUR]
    choix = max(admissibles, key=cle_selection)
    return choix, candidats


def cle_selection(m):
    return (m["fuites_detectees_en_5min"], m["fuites_detectees"],
            -m["fausses_alertes"], m["f1_minutes"], m["seuil"])


def poids_evenements(mesures, y):
    poids = np.ones(len(y), dtype=float)
    groupes = mesures.groupby("scenario_id", sort=False).indices
    compte = {nom: int(np.count_nonzero(y[indices])) for nom, indices in groupes.items()}
    n_evenements = sum(n > 0 for n in compte.values())
    total_positifs = int(np.count_nonzero(y))
    for nom, indices in groupes.items():
        if compte[nom]:
            positifs = indices[np.asarray(y)[indices] == 1]
            masse = total_positifs / n_evenements
            debut, suite = positifs[:5], positifs[5:]
            if len(suite):
                poids[debut] = masse * 0.5 / len(debut)
                poids[suite] = masse * 0.5 / len(suite)
            else:
                poids[debut] = masse / len(debut)
    return poids


def afficher(titre, metriques):
    print(f"\n{titre}")
    print(f"Fuites detectees : {metriques['fuites_detectees']}/{metriques['fuites_total']}")
    print(f"Fuites detectees en 5 min : {metriques['fuites_detectees_en_5min']}/{metriques['fuites_total']}")
    print(f"Fausses alertes : {metriques['fausses_alertes']} ({metriques['fausses_alertes_par_jour']:.3f}/jour simule)")
    print(f"Cas sains avec alerte : {metriques['cas_sains_avec_alerte']}")
    print(f"Minutes en alerte hors fuite : {metriques['minutes_alerte_hors_fuite']}")
    print(f"F1 par minute : {metriques['f1_minutes']:.3f}")
    print(f"Delai median / maximum : {metriques['delai_median_s']} / {metriques['delai_max_s']} s")


def entrainer(dataset):
    dataset = dataset.resolve()
    if not (dataset / "_SUCCESS").is_file():
        raise ValueError("Dataset incomplet : marqueur _SUCCESS absent.")
    audit = json.loads((dataset / "resume_dataset.json").read_text(encoding="utf-8"))
    if audit["verifications"] != "PASS":
        raise ValueError("Dataset non valide.")
    print("Lecture de train et validation uniquement...", flush=True)
    train = lire_mesures(dataset, "train", audit)
    validation = lire_mesures(dataset, "validation", audit)
    ids_train, ids_validation = set(train.scenario_id), set(validation.scenario_id)
    if ids_train & ids_validation:
        raise ValueError("Un scenario est partage entre train et validation.")
    y_train = lire_etiquettes(dataset, "train", audit, train)
    y_validation = lire_etiquettes(dataset, "validation", audit, validation)
    if set(y_train) != {0, 1}:
        raise ValueError("Train doit contenir des minutes saines et avec fuite.")
    assets, sha_reseau = geometrie_fixe()
    calibrer_residus(train, y_train, assets)
    x_train, x_validation = variables(train, assets), variables(validation, assets)
    print(f"Entrainement : {len(train)} mesures, {len(x_train.columns)} variables...", flush=True)
    poids = poids_evenements(train, y_train)
    meilleur = None
    candidats, configurations = [], []
    for configuration in CONFIGURATIONS:
        nom = configuration["nom"]
        parametres = dict(RF_PARAMETRES, **{k: v for k, v in configuration.items() if k != "nom"})
        print(f"Apprentissage et validation : {nom}", flush=True)
        modele_courant = RandomForestClassifier(**parametres)
        modele_courant.fit(x_train, y_train, sample_weight=poids)
        scores_courants = modele_courant.predict_proba(x_validation)[:, list(modele_courant.classes_).index(1)]
        choix_courant, seuils_courants = choisir_seuil(validation, scores_courants, y_validation)
        configurations.append(dict(choix_courant, configuration=nom, parametres=parametres))
        candidats.extend(dict(m, configuration=nom) for m in seuils_courants)
        if meilleur is None or cle_selection(choix_courant) > cle_selection(meilleur[0]):
            meilleur = (choix_courant, modele_courant, scores_courants, parametres, nom)
    choix, modele, scores, parametres, nom_configuration = meilleur
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = DOSSIER / "models" / "aqua_ml_v1" / identifiant
    run.mkdir(parents=True)
    bundle = {"modele": modele, "geometrie": assets, "variables": list(x_train.columns),
              "seuil": choix["seuil"], "version_sklearn": sklearn.__version__}
    joblib.dump(bundle, run / "modele.joblib", compress=3)
    pd.DataFrame(candidats).to_csv(run / "seuils_validation.csv", index=False)
    prediction_validation = predictions(validation, scores, choix["seuil"])
    prediction_validation.to_csv(run / "predictions_validation.csv", index=False)
    _, evenements_validation, scenarios_validation = evaluer_alertes(prediction_validation, y_validation)
    pd.DataFrame(evenements_validation).to_csv(run / "evenements_validation.csv", index=False)
    pd.DataFrame(scenarios_validation).to_csv(run / "scenarios_validation.csv", index=False)
    ecrire_json(run / "metriques_validation.json", choix)
    ecrire_json(run / "entrainement.json", {
        "origine": "simulation", "dataset": str(dataset), "audit_dataset": audit,
        "sha256_modele": sha256(run / "modele.joblib"), "sha256_code": sha256(__file__),
        "sha256_reseau_initial": sha_reseau,
        "python": platform.python_version(), "sklearn": sklearn.__version__,
        "numpy": np.__version__, "pandas": pd.__version__,
        "parametres_rf": parametres, "configuration": nom_configuration,
        "configurations_comparees_sur_validation": configurations,
        "ponderation_train": "poids positifs egaux par evenement ; moitie sur les 5 premieres minutes",
        "seuil": choix["seuil"],
        "budget_fa_validation_par_jour": BUDGET_FA_JOUR,
        "variables": list(x_train.columns), "ids_train": sorted(ids_train),
        "ids_validation": sorted(ids_validation), "test_utilise_pour_reglage": False,
        "limite": "Maquette synthetique unique, petit nombre d'evenements, probabilites non calibrees.",
    })
    (run / "_ENTRAINE").write_text("PASS\n", encoding="utf-8")
    afficher("VALIDATION (sert au choix du seuil)", choix)
    print(f"Configuration retenue : {nom_configuration}")
    print(f"Seuil fige : {choix['seuil']:.2f}")
    print(f"Modele : {run}")
    return run


def tracer_test(prediction, y, chemin):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    table = prediction.copy()
    table["vrai"] = y
    groupes = list(table.groupby("scenario_id", sort=False))
    fig, axes = plt.subplots(2, 1, figsize=(12, max(8, len(groupes) * 0.55)), sharex=True, sharey=True)
    etiquettes = [f"Essai {i+1:02d}" for i in range(len(groupes))]
    for ax, colonne, titre, couleur in [(axes[0], "vrai", "Fuites simulees", "#087f8c"),
                                      (axes[1], "alerte", "Alertes du modele ML", "#c45531")]:
        for i, (_, groupe) in enumerate(groupes):
            masque = groupe[colonne].to_numpy(dtype=bool)
            temps = groupe.temps_s.to_numpy()
            debuts = np.flatnonzero(masque & ~np.r_[False, masque[:-1]])
            fins = np.flatnonzero(masque & ~np.r_[masque[1:], False])
            for debut, fin in zip(debuts, fins):
                ax.broken_barh([(temps[debut] / 3600, (temps[fin] + PAS - temps[debut]) / 3600)],
                               (i - 0.3, 0.6), facecolors=couleur)
        ax.set_yticks(range(len(groupes)), etiquettes)
        ax.set_xlim(0, 24)
        ax.set_xticks(range(0, 25, 2))
        ax.set_title(titre)
        ax.grid(axis="x", alpha=0.2)
        ax.set_axisbelow(True)
    axes[0].invert_yaxis()
    axes[1].set_xlabel("Temps simule (heures)")
    fig.suptitle("AQUA Sentinel | Evaluation ML sur les scenarios de test")
    fig.tight_layout()
    fig.savefig(chemin, dpi=160)
    plt.close(fig)


def evaluer(run):
    if not (run / "_ENTRAINE").is_file():
        raise ValueError("Modele incomplet.")
    meta = json.loads((run / "entrainement.json").read_text(encoding="utf-8"))
    verifier_empreinte(Path(__file__), meta["sha256_code"])
    verifier_empreinte(run / "modele.joblib", meta["sha256_modele"])
    if sklearn.__version__ != meta["sklearn"]:
        raise ValueError("La version de scikit-learn doit correspondre a celle de l'entrainement.")
    if (run / "_TEST_EVALUE").is_file():
        afficher("TEST DEJA EVALUE : resultat conserve", json.loads((run / "metriques_test.json").read_text()))
        print(f"Resultats : {run}")
        return
    bundle = joblib.load(run / "modele.joblib")
    dataset = Path(meta["dataset"])
    test = lire_mesures(dataset, "test", meta["audit_dataset"])
    if set(test.scenario_id) & (set(meta["ids_train"]) | set(meta["ids_validation"])):
        raise ValueError("Un scenario de test a deja ete utilise pendant l'apprentissage.")
    x_test = variables(test, bundle["geometrie"])
    if list(x_test.columns) != bundle["variables"]:
        raise ValueError("Variables differentes entre apprentissage et prediction.")
    scores = bundle["modele"].predict_proba(x_test)[:, list(bundle["modele"].classes_).index(1)]
    prediction = predictions(test, scores, bundle["seuil"])
    prediction.to_csv(run / "predictions_test.csv", index=False)
    # Premier acces aux etiquettes de test : apres les predictions sauvegardees.
    y_test = lire_etiquettes(dataset, "test", meta["audit_dataset"], test)
    metriques, evenements, scenarios = evaluer_alertes(prediction, y_test)
    metriques.update({"origine": "simulation", "seuil": bundle["seuil"],
                      "test_utilise_pour_reglage": False,
                      "limite": "Maquette synthetique unique, peu d'evenements, aucune validation terrain."})
    ecrire_json(run / "metriques_test.json", metriques)
    pd.DataFrame(evenements).to_csv(run / "evenements_test.csv", index=False)
    pd.DataFrame(scenarios).to_csv(run / "scenarios_test.csv", index=False)
    tracer_test(prediction, y_test, run / "bilan_test.png")
    verifier_empreinte(run / "modele.joblib", meta["sha256_modele"])
    (run / "_TEST_EVALUE").write_text("PASS technique\n", encoding="utf-8")
    afficher("TEST FINAL (aucun reglage sur ce jeu)", metriques)
    print("ML AQUA : PASS technique. La qualite est donnee par les mesures ci-dessus.")
    print(f"Resultats : {run}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["entrainer", "evaluer"])
    parser.add_argument("--donnees", type=Path)
    parser.add_argument("--modele", type=Path)
    args = parser.parse_args()
    if args.action == "entrainer":
        dataset = args.donnees or dernier_dossier(DOSSIER / "data" / "aqua_dataset_v1", "_SUCCESS")
        entrainer(dataset)
    else:
        run = args.modele or dernier_dossier(DOSSIER / "models" / "aqua_ml_v1", "_ENTRAINE")
        evaluer(run.resolve())


if __name__ == "__main__":
    main()
