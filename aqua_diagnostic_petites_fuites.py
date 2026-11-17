r"""AQUA Sentinel : diagnostic avant une prochaine amelioration.

  .venv\Scripts\python.exe aqua_diagnostic_petites_fuites.py
  .venv\Scripts\python.exe aqua_diagnostic_petites_fuites.py --modele CHEMIN_V2

Prerequis : V2 entrainee et evaluee. Memes dependances que la V2.
Ce script lit les CAPTEURS et ETIQUETTES de train et validation seulement.
Le protocole de generation fournit les ouvertures/tuyaux des cas analyses ;
ces descripteurs vrais servent uniquement au bilan, jamais au calcul d'alerte.
Les capteurs et etiquettes des deux jeux de test connus ne sont pas ouverts.
Il n'y a aucun nouvel apprentissage ou changement de modele/seuil V2.

Temoin physique fixe, sans recherche de seuil sur validation :
1. Residu de boucle = residu_P2 + residu_P4 - residu_P3 - residu_P5,
   en metres de charge hydraulique. Les pressions mesurees s'annulent dans
   cette combinaison ; la geometrie et la rugosite sont supposees connues.
2. Moyenne causale sur 5 mesures. Centre et dispersion robuste (MAD mise
   a l'echelle normale) estimes seulement sur les fenetres entierement
   saines de train, avec cinq observations completes.
3. Score = valeur absolue de l'ecart a ce centre. Seuil = 4 dispersions.
   Ce facteur est une regle d'essai, pas une garantie de fausse alerte.
4. Meme operateur d'alarme que V1/V2 : moyenne causale sur 3 scores,
   3 confirmations pour ouvrir, 3 sous le demi-seuil pour fermer.
   Les quatre premieres mesures de chaque jour ne peuvent pas declencher.

Les scores hydrauliques sont des metres, pas des probabilites. Les scores
ML de la V2 ne sont pas non plus des probabilites calibrees.
Le rapport compare ce temoin et la V2 deja choisie sur validation ; il ne
constitue pas un nouveau test independant. Deux fuites par ouverture dans
la validation par defaut : echantillon trop petit pour une garantie.

Sorties dans resultats_aqua/diagnostic_petites_fuites/<identifiant> :
resume_diagnostic.json, comparaison_validation.csv, cas_validation.csv,
predictions_ml_validation.csv, predictions_temoin_validation.csv.
Le ratio signal/dispersion par cas est un DESCRIPTIF apres observation
de la fuite ; il n'est pas une variable de prediction ou une limite physique.

Sources :
https://usepa.github.io/EPANET2.2/3_network_model.html
https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.median_abs_deviation.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
from scipy.stats import median_abs_deviation

import aqua_ml_v1 as v1
import aqua_ml_v2 as v2


DOSSIER = Path(__file__).resolve().parent
FENETRE = 5
FACTEUR = 4.0


def signal_boucle(mesures, geometrie):
    """Present et passe, separement par scenario ; aucune etiquette."""
    residus = v1.residus_hydrauliques(mesures, geometrie)
    brut = residus.P2 + residus.P4 - residus.P3 - residus.P5
    moyen = np.full(len(mesures), np.nan)
    complet = np.zeros(len(mesures), dtype=bool)
    for indices in mesures.groupby("scenario_id", sort=False).indices.values():
        moyen[indices] = brut.iloc[indices].rolling(FENETRE, min_periods=FENETRE, center=False).mean().to_numpy()
        complet[indices] = np.arange(len(indices)) >= FENETRE - 1
    return moyen, complet


def calibrer_temoin(mesures, y, geometrie):
    moyen, complet = signal_boucle(mesures, geometrie)
    sain = np.zeros(len(mesures), dtype=bool)
    for indices in mesures.groupby("scenario_id", sort=False).indices.values():
        presents = pd.Series(np.asarray(y)[indices]).rolling(FENETRE, min_periods=FENETRE).sum().to_numpy()
        sain[indices] = presents == 0
    echantillon = moyen[sain & complet]
    if len(echantillon) < 100 or not np.isfinite(echantillon).all():
        raise ValueError("Pas assez de fenetres saines de train pour le temoin.")
    centre = float(np.median(echantillon))
    dispersion = max(1e-9, float(median_abs_deviation(echantillon, scale="normal")))
    return {"centre_m": centre, "dispersion_robuste_m": dispersion,
            "seuil_m": FACTEUR * dispersion, "facteur": FACTEUR,
            "fenetre_mesures": FENETRE, "fenetres_calibration": len(echantillon),
            "jeu_calibration": "train, fenetres de cinq minutes saines uniquement"}


def predire_temoin(mesures, geometrie, calibration):
    moyen, complet = signal_boucle(mesures, geometrie)
    score = np.where(complet, np.abs(moyen - calibration["centre_m"]), 0.0)
    prediction = v1.predictions(mesures, score, calibration["seuil_m"])
    return prediction.rename(columns={"score_fuite": "ecart_boucle_m", "score_lisse": "ecart_boucle_lisse_m"}), moyen


def details_evenements(evenements_ml, evenements_temoin, mesures, moyen, calibration, configuration):
    ml = pd.DataFrame(evenements_ml).rename(columns={"detectee": "detectee_ml", "delai_s": "delai_ml_s", "detectee_en_5min": "ml_en_5min"})
    ph = pd.DataFrame(evenements_temoin).rename(columns={"detectee": "detectee_temoin", "delai_s": "delai_temoin_s", "detectee_en_5min": "temoin_en_5min"})
    cles = ["scenario_id", "debut_fuite_s", "fin_fuite_s"]
    table = ml.merge(ph, on=cles, validate="one_to_one")
    cas = {x["scenario_id"]: x for x in configuration if x["partie"] == "validation"}
    table["tuyau_cible"] = [cas[s]["tuyau_cible"] for s in table.scenario_id]
    table["ouverture_mm"] = [cas[s]["diametre_fuite_mm"] for s in table.scenario_id]
    table["delai_ml_min"] = table.delai_ml_s / 60
    table["delai_temoin_min"] = table.delai_temoin_s / 60
    ratios = []
    for _, evenement in table.iterrows():
        masque = (mesures.scenario_id.eq(evenement.scenario_id)
                  & mesures.temps_s.ge(evenement.debut_fuite_s)
                  & mesures.temps_s.lt(evenement.fin_fuite_s))
        signal = float(np.nanmedian(moyen[masque.to_numpy()]))
        ratios.append(abs(signal - calibration["centre_m"]) / calibration["dispersion_robuste_m"])
    table["ratio_signal_dispersion_descriptif"] = ratios
    return table.sort_values(["ouverture_mm", "tuyau_cible", "scenario_id"]).reset_index(drop=True)


def diagnostiquer(modele_v2):
    modele_v2 = modele_v2.resolve()
    if not (modele_v2 / "_TEST_EVALUE").is_file():
        raise ValueError("Utiliser une V2 dont le bilan de test est deja conserve.")
    meta = json.loads((modele_v2 / "entrainement.json").read_text(encoding="utf-8"))
    v2.verifier_gel(modele_v2, meta)
    bundle = joblib.load(modele_v2 / "modele.joblib")
    dataset = Path(meta["dataset"])
    audit = meta["audit_dataset"]
    print("Capteurs et etiquettes lus : train et validation uniquement.", flush=True)
    train = v1.lire_mesures(dataset, "train", audit)
    y_train = v1.lire_etiquettes(dataset, "train", audit, train)
    validation = v1.lire_mesures(dataset, "validation", audit)
    y_validation = v1.lire_etiquettes(dataset, "validation", audit, validation)
    if set(train.scenario_id) & set(validation.scenario_id):
        raise ValueError("Scenario partage entre train et validation.")
    calibration = calibrer_temoin(train, y_train, bundle["geometrie"])
    prediction_temoin, moyen = predire_temoin(validation, bundle["geometrie"], calibration)
    x = v2.variables(validation, bundle["geometrie"])
    if list(x.columns) != bundle["variables"]:
        raise ValueError("Variables V2 incoherentes.")
    prediction_ml = v1.predictions(validation, v2.scores(bundle["modele"], x), bundle["seuil"])
    conserve = pd.read_csv(modele_v2 / "predictions_validation.csv")
    if not conserve[v1.CLES].equals(prediction_ml[v1.CLES]):
        raise ValueError("Le bilan de validation conserve a des cles differentes.")
    if not np.array_equal(conserve.alerte.to_numpy(), prediction_ml.alerte.to_numpy()):
        raise ValueError("Les alertes de validation V2 ne sont plus reproductibles.")
    if not np.allclose(conserve.score_fuite, prediction_ml.score_fuite, rtol=1e-10, atol=1e-12):
        raise ValueError("Les scores de validation V2 ne sont plus reproductibles.")
    metriques_ml, evenements_ml, _ = v1.evaluer_alertes(prediction_ml, y_validation)
    metriques_temoin, evenements_temoin, _ = v1.evaluer_alertes(prediction_temoin, y_validation)
    # Descripteurs de validation reserves a l'analyse apres prediction.
    configuration = [c for c in json.loads((dataset / "protocole.json").read_text(encoding="utf-8"))["scenarios"]
                     if c["partie"] == "validation"]
    details = details_evenements(evenements_ml, evenements_temoin, validation, moyen, calibration, configuration)
    ouvertures = details.groupby("ouverture_mm", sort=True).agg(
        fuites=("scenario_id", "size"), ml_detectees=("detectee_ml", "sum"),
        ml_en_5min=("ml_en_5min", "sum"), temoin_detectees=("detectee_temoin", "sum"),
        temoin_en_5min=("temoin_en_5min", "sum")).reset_index()
    comparaison = pd.DataFrame([dict(methode="ML_V2", **metriques_ml),
                               dict(methode="Temoin_physique_4sigma", **metriques_temoin)])
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = DOSSIER / "resultats_aqua" / "diagnostic_petites_fuites" / identifiant
    run.mkdir(parents=True)
    details.to_csv(run / "cas_validation.csv", index=False)
    ouvertures.to_csv(run / "ouvertures_validation.csv", index=False)
    comparaison.to_csv(run / "comparaison_validation.csv", index=False)
    prediction_ml.to_csv(run / "predictions_ml_validation.csv", index=False)
    prediction_temoin.to_csv(run / "predictions_temoin_validation.csv", index=False)
    v2.verifier_gel(modele_v2, meta)
    resume = {"origine": "simulation", "modele_v2": str(modele_v2), "dataset": str(dataset),
              "sha256_diagnostic": v1.sha256(__file__), "sources_modele_sha256": meta["sources_sha256"],
              "sha256_modele_v2": meta["sha256_modele_v2"],
              "jeux_capteurs_et_etiquettes_lus": ["train", "validation"],
              "calibration_temoin": calibration, "validation_ml_v2": metriques_ml,
              "validation_temoin": metriques_temoin, "par_ouverture": ouvertures.to_dict(orient="records"),
              "modele_v2_modifie": False, "test_reexecute": False,
              "limite": "Analyse exploratoire de validation, pas un nouveau test. Peu de fuites par ouverture ; maquette unique et bruit suppose.",
              "prochain_critere": "Ameliorer la detection en cinq minutes des ouvertures 4-6 mm ; validation pour les reglages, test neuf pour le prochain bilan."}
    v1.ecrire_json(run / "resume_diagnostic.json", resume)
    (run / "_SUCCESS").write_text("PASS technique\n", encoding="utf-8")
    print("\nPAR OUVERTURE - VALIDATION SEULEMENT")
    print(ouvertures.to_string(index=False))
    print("\nCAS DE VALIDATION")
    print(details[["tuyau_cible", "ouverture_mm", "delai_ml_min", "delai_temoin_min",
                   "ratio_signal_dispersion_descriptif"]].round(2).to_string(index=False))
    v1.afficher("VALIDATION : ML V2 conserve", metriques_ml)
    v1.afficher("VALIDATION : temoin physique fixe, seuil calibre sur train", metriques_temoin)
    print(f"\nSeuil du temoin : {calibration['seuil_m']:.6f} m de charge hydraulique.")
    print("Ce seuil n'est ni une pression minimale ni une ouverture de fuite.")
    print("DIAGNOSTIC : PASS technique ; aucun nouveau test, aucune modification de V2.")
    print(f"Resultats : {run}")
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modele", type=Path, help="Dossier de la V2 deja evaluee")
    args = parser.parse_args()
    modele = args.modele or v1.dernier_dossier(DOSSIER / "models" / "aqua_ml_v2", "_TEST_EVALUE")
    diagnostiquer(modele)


if __name__ == "__main__":
    main()
