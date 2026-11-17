r"""AQUA Sentinel : etude de capteurs VIRTUELS sur train/validation.

  .venv\Scripts\python.exe aqua_etude_capteurs_v1.py
  .venv\Scripts\python.exe aqua_etude_capteurs_v1.py --modele CHEMIN_MODELE_V2

Prerequis : les scripts demo, dataset V1, ML V1/V2 et une V2 deja evaluee.
Aucune nouvelle dependance. Le premier lancement rejoue les 48 jours de
train et les 16 jours de validation du jeu par defaut. Aucun ancien ou
nouveau scenario de test n'est simule ou lu.

Objectif : mesurer l'effet de DEBITS SUPPLEMENTAIRES et non annoncer une
amelioration du ML avec les memes entrees. Les neuf mesures actuelles
(4 pressions, 5 debits) sont conservees a l'identique. Nous ajoutons les
debits des troncons aval de P2, P3, P4, P5, lus dans les sorties du solveur.
Leur bruit gaussien suppose est independant, sigma 0.05 L/s comme les
debits existants, graine SeedSequence([seed_scenario, 3]). Les quatre
debitmetres sont generes dans TOUS les cas, y compris sains ; le calcul
ne choisit jamais un capteur a partir de la vraie cible de fuite.

Chaque simulation reproduit exactement les anciens parametres physiques
et le bruit des neuf capteurs existants. Les observations reproduites sont
verifiees contre les CSV originaux avant d'accepter les nouveaux debits.
Les debits aval viennent du simulateur, JAMAIS d'un calcul utilisant le
debit vrai de fuite. La verite sert seulement aux audits physiques et au
bilan separe. Cache des nouvelles mesures dans data/aqua_etude_capteurs_v1.

Regle physique : pour chaque paire, bilan = q_amont - q_aval, en L/s.
Avec nos sens de reference fixes, ce bilan est egal au soutirage entre
les deux points de mesure, meme en cas d'inversion du courant. Ici, aucun
branchement legitime ni stockage n'est present entre eux. Cette hypothese
est ESSENTIELLE : un soutirage legitime serait sinon indiscernable d'une
fuite par ce seul bilan. Les mesures doivent etre synchronisees.

Moyenne causale de 3 bilans, centre et MAD normalisee appris uniquement
sur les fenetres de 3 mesures entierement saines de train. Seuil positif
fixe = 4 dispersions ; pas de recherche de seuil sur validation. Meme
alarme que V1/V2 : moyenne de 3 scores puis 3 confirmations d'ouverture,
3 sous le demi-seuil pour fermer. Les deux premieres mesures sont neutres.
Une alerte globale de bilan est l'OU logique des quatre alertes par tuyau.

Configurations de validation :
 ML_9      : V2 avec ses 9 capteurs actuels.
 ML_11     : OU entre V2 et les bilans P4/P5, 2 capteurs aval ajoutes.
 ML_13     : OU entre V2 et les quatre bilans, 4 capteurs aval ajoutes.
 Bilans_13 : quatre bilans seuls, meme installation de 13 capteurs.
Le dernier calcul exploite seulement 8 des 13 mesures disponibles : les
debits amont et aval des 4 tuyaux. Le compte indique l'installation garde
dans cette comparaison, pas le minimum de mesures exige par l'algorithme.

Sorties : resultats_aqua/etude_capteurs_v1/<identifiant>/resume_etude.json,
comparaison_validation.csv, ouvertures_validation.csv, cas_validation.csv,
predictions_*_validation.csv, calibration_bilans.json.

Limites : etude exploratoire sur la validation deja connue, pas un test
independant. Meme maquette, fuite unique, positions connues entre paires,
bruit suppose independant, sans biais, derive, decalage d'horloge ou panne.
Les couts et la faisabilite terrain ne sont pas evalues. Aucun achat,
capteur reel ou nouvelle installation physique n'est implique. Localisation
ML non entrainee. La configuration choisie devra passer un NOUVEAU test.

Sources :
https://usepa.github.io/EPANET2.2/12_analysis_algorithms.html
https://usepa.github.io/WNTR/units.html
https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.median_abs_deviation.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
import wntr
from scipy.stats import median_abs_deviation

import aqua_ml_v1 as v1
import aqua_ml_v2 as v2
import aqua_dataset_v1 as generateur


DOSSIER = Path(__file__).resolve().parent
TUYAUX = ["P2", "P3", "P4", "P5"]
EXTRA = [f"debit_{p}_aval_m3_s" for p in TUYAUX]
FENETRE = 3
FACTEUR = 4.0
CONFIGURATIONS = [
    dict(nom="ML_9", capteurs=9, utiliser_ml=True, paires=[]),
    dict(nom="ML_11", capteurs=11, utiliser_ml=True, paires=["P4", "P5"]),
    dict(nom="ML_13", capteurs=13, utiliser_ml=True, paires=TUYAUX),
    dict(nom="Bilans_13", capteurs=13, utiliser_ml=False, paires=TUYAUX),
]


def sources():
    return dict(v2.sources(), **{Path(__file__).name: v1.sha256(__file__)})


def mesurer_aval(cas, ancien):
    """Mesurer tous les troncons aval ; aucune alerte dans ce generateur."""
    if cas["partie"] not in ["train", "validation"]:
        raise ValueError("Cette etude n'autorise que train et validation.")
    reseau = generateur.construire_reseau(cas)
    resultat = wntr.sim.WNTRSimulator(reseau).run_sim()
    calendrier = np.arange(0, generateur.DUREE + 1, generateur.PAS)
    if not np.array_equal(resultat.link["flowrate"].index, calendrier):
        raise ValueError("Rejeu hydraulique incomplet.")
    n = generateur.LIGNES
    bruit = np.random.default_rng(np.random.SeedSequence([cas["seed"], 2]))
    ecart_reproduction = 0.0
    for colonne in v1.CAPTEURS:
        if colonne.startswith("pression_"):
            propre = resultat.node["pressure"][colonne.split("_")[1]].iloc[:n].to_numpy()
            sigma = generateur.BRUIT_PRESSION_M
        else:
            propre = resultat.link["flowrate"][colonne.split("_")[1]].iloc[:n].to_numpy()
            sigma = generateur.BRUIT_DEBIT_M3_S
        reproduit = propre + bruit.normal(0, sigma, n)
        ecart = float(np.max(np.abs(reproduit - ancien[colonne].to_numpy())))
        ecart_reproduction = max(ecart_reproduction, ecart)
        if not np.allclose(reproduit, ancien[colonne], rtol=1e-9, atol=1e-10):
            raise ValueError("Rejeu different des anciennes mesures : " + cas["scenario_id"] + " " + colonne)
    sorties = ancien[v1.CLES].copy().reset_index(drop=True)
    bruit_aval = np.random.default_rng(np.random.SeedSequence([cas["seed"], 3]))
    erreur_paires = 0.0
    for tuyau in TUYAUX:
        aval_propre = resultat.link["flowrate"][tuyau + "_aval"].iloc[:n].to_numpy()
        sorties[f"debit_{tuyau}_aval_m3_s"] = aval_propre + bruit_aval.normal(0, generateur.BRUIT_DEBIT_M3_S, n)
        # Audit seulement : la verite ne sert pas a construire les mesures.
        erreur = (resultat.link["flowrate"][tuyau] - resultat.link["flowrate"][tuyau + "_aval"]
                  - resultat.node["leak_demand"]["L" + tuyau[1:]])
        erreur_paires = max(erreur_paires, float(erreur.abs().max()))
    if not np.isfinite(sorties[EXTRA].to_numpy()).all() or erreur_paires > 1e-6:
        raise ValueError("Debits aval ou bilan physique invalides.")
    return sorties, ecart_reproduction, erreur_paires


def obtenir_mesures(dataset, meta, originaux, plan):
    signature = {"sources_sha256": sources(), "wntr": wntr.__version__,
                 "capteurs_originaux": {p: meta["audit_dataset"]["jeux"][p]["sha256_capteurs"] for p in originaux},
                 "bruit_aval_l_s": generateur.BRUIT_DEBIT_M3_S * 1000, "graine_aval": "[seed_scenario, 3]"}
    code = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
    cache = DOSSIER / "data" / "aqua_etude_capteurs_v1" / code
    cache.mkdir(parents=True, exist_ok=True)
    if not (cache / "_SUCCESS").is_file():
        v1.ecrire_json(cache / "protocole.json", dict(signature, dataset=str(dataset),
                       scenarios=[c for c in plan if c["partie"] in originaux]))
        blocs = {partie: [] for partie in originaux}
        groupes = {partie: {s: g.reset_index(drop=True) for s, g in x.groupby("scenario_id", sort=False)}
                   for partie, x in originaux.items()}
        cas_autorises = [c for c in plan if c["partie"] in originaux]
        erreur_mesures = erreur_bilan = 0.0
        for numero, cas in enumerate(cas_autorises, 1):
            print(f"[{numero}/{len(cas_autorises)}] Rejeu {cas['partie']} : {cas['scenario_id']}", flush=True)
            observations, erreur, bilan = mesurer_aval(cas, groupes[cas["partie"]][cas["scenario_id"]])
            blocs[cas["partie"]].append(observations)
            erreur_mesures = max(erreur_mesures, erreur)
            erreur_bilan = max(erreur_bilan, bilan)
        empreintes = {}
        for partie, tables in blocs.items():
            table = pd.concat(tables, ignore_index=True).sort_values(v1.CLES).reset_index(drop=True)
            if not table[v1.CLES].equals(originaux[partie][v1.CLES]):
                raise ValueError("Nouveaux capteurs mal alignes : " + partie)
            cible = cache / (partie + ".csv")
            temporaire = cible.with_suffix(".csv.tmp")
            table.to_csv(temporaire, index=False)
            temporaire.replace(cible)
            empreintes[partie] = v1.sha256(cible)
        v1.ecrire_json(cache / "audit.json", {"signature": signature, "empreintes": empreintes,
            "ecart_reproduction_max_unites_natives": erreur_mesures,
            "erreur_bilan_paires_max_m3_s": erreur_bilan, "jeux": list(originaux),
            "verite_utilisee_pour_construire_debits_aval": False})
        (cache / "_SUCCESS").write_text("PASS technique\n", encoding="utf-8")
    audit = json.loads((cache / "audit.json").read_text(encoding="utf-8"))
    if audit["signature"] != signature:
        raise ValueError("Cache incompatible avec les sources ou les anciennes mesures.")
    enrichis = {}
    for partie, original in originaux.items():
        cible = cache / (partie + ".csv")
        v1.verifier_empreinte(cible, audit["empreintes"][partie])
        extra = pd.read_csv(cible).sort_values(v1.CLES).reset_index(drop=True)
        if list(extra.columns) != v1.CLES + EXTRA or not extra[v1.CLES].equals(original[v1.CLES]):
            raise ValueError("Schema ou cles des capteurs supplementaires invalides.")
        if not np.isfinite(extra[EXTRA].to_numpy()).all():
            raise ValueError("Capteur aval absent ou non fini.")
        enrichis[partie] = original.merge(extra, on=v1.CLES, validate="one_to_one")
    return enrichis, cache, audit


def bilans_lisses(mesures):
    sorties = pd.DataFrame(index=mesures.index, columns=TUYAUX, dtype=float)
    complet = np.zeros(len(mesures), dtype=bool)
    for indices in mesures.groupby("scenario_id", sort=False).indices.values():
        for p in TUYAUX:
            ecart = (mesures.iloc[indices][f"debit_{p}_m3_s"]
                     - mesures.iloc[indices][f"debit_{p}_aval_m3_s"]) * 1000
            sorties.loc[indices, p] = ecart.rolling(FENETRE, min_periods=FENETRE, center=False).mean().to_numpy()
        complet[indices] = np.arange(len(indices)) >= FENETRE - 1
    return sorties, complet


def calibrer_bilans(train, y_train):
    bilans, complet = bilans_lisses(train)
    sain = np.zeros(len(train), dtype=bool)
    for indices in train.groupby("scenario_id", sort=False).indices.values():
        somme = pd.Series(np.asarray(y_train)[indices]).rolling(FENETRE, min_periods=FENETRE).sum()
        sain[indices] = somme.to_numpy() == 0
    normal = bilans.loc[sain & complet]
    if len(normal) < 100 or not np.isfinite(normal.to_numpy()).all():
        raise ValueError("Pas assez de fenetres saines de train pour calibrer les paires.")
    return {p: {"centre_l_s": float(normal[p].median()),
                "dispersion_l_s": max(1e-9, float(median_abs_deviation(normal[p], scale="normal"))),
                "facteur": FACTEUR,
                "seuil_l_s": FACTEUR * max(1e-9, float(median_abs_deviation(normal[p], scale="normal")))} for p in TUYAUX}


def predire_bilans(mesures, calibration):
    bilans, complet = bilans_lisses(mesures)
    alertes = pd.DataFrame(index=mesures.index)
    for p in TUYAUX:
        score = np.where(complet, bilans[p].to_numpy() - calibration[p]["centre_l_s"], 0.0)
        alertes[p] = v1.predictions(mesures, score, calibration[p]["seuil_l_s"]).alerte.to_numpy()
    return alertes


def combiner(mesures, prediction_ml, alertes_bilans, configuration):
    resultat = mesures[v1.CLES].copy()
    alerte = np.zeros(len(mesures), dtype=bool)
    if configuration["utiliser_ml"]:
        if not resultat[v1.CLES].equals(prediction_ml[v1.CLES]):
            raise ValueError("Alertes ML mal alignees.")
        alerte |= prediction_ml.alerte.to_numpy(dtype=bool)
    if configuration["paires"]:
        alerte |= alertes_bilans[configuration["paires"]].any(axis=1).to_numpy()
    resultat["alerte"] = alerte.astype(int)
    return resultat


def etudier(modele_v2):
    modele_v2 = modele_v2.resolve()
    if not (modele_v2 / "_TEST_EVALUE").is_file():
        raise ValueError("Utiliser une V2 dont le bilan est conserve.")
    meta = json.loads((modele_v2 / "entrainement.json").read_text(encoding="utf-8"))
    v2.verifier_gel(modele_v2, meta)
    empreinte_sources = sources()
    dataset = Path(meta["dataset"])
    originaux, etiquettes = {}, {}
    for partie in ["train", "validation"]:
        originaux[partie] = v1.lire_mesures(dataset, partie, meta["audit_dataset"])
        etiquettes[partie] = v1.lire_etiquettes(dataset, partie, meta["audit_dataset"], originaux[partie])
    if set(originaux["train"].scenario_id) & set(originaux["validation"].scenario_id):
        raise ValueError("Scenario partage entre train et validation.")
    plan = [c for c in json.loads((dataset / "protocole.json").read_text(encoding="utf-8"))["scenarios"]
            if c["partie"] in originaux]
    for partie, table in originaux.items():
        if {c["scenario_id"] for c in plan if c["partie"] == partie} != set(table.scenario_id):
            raise ValueError("Plan de rejeu different des scenarios observes.")
    print("Etude appariee : train/validation seulement, memes observations existantes.", flush=True)
    enrichis, cache, audit_cache = obtenir_mesures(dataset, meta, originaux, plan)
    calibration = calibrer_bilans(enrichis["train"], etiquettes["train"])
    bundle = joblib.load(modele_v2 / "modele.joblib")
    x = v2.variables(originaux["validation"], bundle["geometrie"])
    if list(x.columns) != bundle["variables"]:
        raise ValueError("Variables ML V2 incompatibles.")
    prediction_ml = v1.predictions(originaux["validation"], v2.scores(bundle["modele"], x), bundle["seuil"])
    conserve = pd.read_csv(modele_v2 / "predictions_validation.csv")
    if not conserve[v1.CLES].equals(prediction_ml[v1.CLES]) or not np.array_equal(conserve.alerte, prediction_ml.alerte):
        raise ValueError("La V2 de reference ne reproduit plus ses alertes de validation.")
    alertes_bilans = predire_bilans(enrichis["validation"], calibration)
    validation = originaux["validation"]
    y_validation = etiquettes["validation"]
    cas_validation = {c["scenario_id"]: c for c in plan if c["partie"] == "validation"}
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = DOSSIER / "resultats_aqua" / "etude_capteurs_v1" / identifiant
    run.mkdir(parents=True)
    comparaison, evenements_tous, resultats = [], [], {}
    for configuration in CONFIGURATIONS:
        nom = configuration["nom"]
        prediction = combiner(validation, prediction_ml, alertes_bilans, configuration)
        prediction.to_csv(run / ("predictions_" + nom + "_validation.csv"), index=False)
        metriques, evenements, _ = v1.evaluer_alertes(prediction, y_validation)
        resultats[nom] = metriques
        comparaison.append(dict(configuration=nom, capteurs_installes=configuration["capteurs"],
                                delai_max_min=metriques["delai_max_s"] / 60 if metriques["delai_max_s"] is not None else None,
                                **metriques))
        for evenement in evenements:
            physique = cas_validation[evenement["scenario_id"]]
            evenements_tous.append(dict(evenement, configuration=nom,
                                       tuyau_cible=physique["tuyau_cible"], ouverture_mm=physique["diametre_fuite_mm"]))
    comparaison = pd.DataFrame(comparaison)
    details = pd.DataFrame(evenements_tous)
    ouvertures = details.groupby(["configuration", "ouverture_mm"], sort=False).agg(
        fuites=("detectee", "size"), detectees=("detectee", "sum"),
        en_5min=("detectee_en_5min", "sum")).reset_index()
    comparaison.to_csv(run / "comparaison_validation.csv", index=False)
    details.to_csv(run / "cas_validation.csv", index=False)
    ouvertures.to_csv(run / "ouvertures_validation.csv", index=False)
    v1.ecrire_json(run / "calibration_bilans.json", calibration)
    v2.verifier_gel(modele_v2, meta)
    if sources() != empreinte_sources:
        raise ValueError("Un code a change pendant l'etude.")
    v1.ecrire_json(run / "resume_etude.json", {
        "origine": "simulation", "jeux": ["train", "validation"], "modele_v2": str(modele_v2),
        "dataset_original": str(dataset), "cache_capteurs": str(cache), "audit_cache": audit_cache,
        "sources_sha256": empreinte_sources, "sha256_modele_v2": meta["sha256_modele_v2"],
        "configurations": CONFIGURATIONS, "calibration_bilans_train": calibration,
        "resultats_validation": resultats, "par_ouverture": ouvertures.to_dict(orient="records"),
        "modele_ml_modifie": False, "jeux_test_lus_ou_rejoues": False,
        "limite": "Validation exploratoire connue, faible effectif ; davantage de capteurs virtuels, pas une amelioration ML a entrees identiques ; aucun biais, panne ou decalage d'horloge simule ; aucune validation terrain.",
        "prochain_jalon": "Figer la configuration retenue, puis evaluer sur de nouveaux scenarios sans reglage sur ce futur test."})
    (run / "_SUCCESS").write_text("PASS technique\n", encoding="utf-8")
    print("\nCOMPARAISON DES CAPTEURS - VALIDATION SEULEMENT")
    print(comparaison[["configuration", "capteurs_installes", "fuites_detectees", "fuites_detectees_en_5min",
                       "fausses_alertes", "minutes_alerte_hors_fuite", "f1_minutes", "delai_max_min"]].round(3).to_string(index=False))
    print("\nPAR OUVERTURE - VALIDATION SEULEMENT")
    print(ouvertures.to_string(index=False))
    print("\nETUDE : PASS technique. Les configurations enrichies utilisent des mesures supplementaires.")
    print("Validation deja connue : configuration a figer AVANT un nouveau test independant.")
    print(f"Resultats : {run}")
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modele", type=Path, help="Dossier de la V2 deja evaluee")
    args = parser.parse_args()
    modele = args.modele or v1.dernier_dossier(DOSSIER / "models" / "aqua_ml_v2", "_TEST_EVALUE")
    etudier(modele)


if __name__ == "__main__":
    main()
