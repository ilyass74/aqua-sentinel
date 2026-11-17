"""AQUA Sentinel V2 : bilan des debits de la branche vers D.

Execution : .venv\\Scripts\\python.exe aqua_detecteur_v2.py
Garder les trois scripts V1 a cote de ce fichier. Aucune dependance nouvelle.

Hypotheses : reseau initial sans stockage, debits signes comme dans le modele,
pas de fuite en amont des sections P4/P5, demandes A/B/C/D proportionnelles,
pression suffisante pour satisfaire les demandes. La reference est une
simulation nominale sans bruit. P1/P4/P5 sont des capteurs de debit virtuels.
Cette V2 surveille un desequilibre vers D ; elle ne localise pas toutes les
fuites et ne remplace pas une estimation independante de la consommation.

Estimation, avec les debits exprimes dans la meme unite :
  demande_ABC = P1 - P4 - P5
  ratio_D_ABC = (P4_reference + P5_reference) / demande_ABC_reference
  residu = (P4 + P5) - ratio_D_ABC * demande_ABC
Sous les hypotheses, le residu estime une sortie d'eau non expliquee.

Calibration : sept cas normaux avec graines distinctes, avant toute lecture
des nouvelles observations de test. Moyenne causale de 5 mesures ; seuil
d'ouverture max(0.10 L/s, 4 * MAD normalisee) apres retrait du biais median.
Le facteur 4 et le plancher sont des choix d'exercice, sans garantie de taux
de fausse alerte. Confirmation : 3 mesures ; fermeture : 3 mesures sous un
seuil egal a la moitie du seuil d'ouverture. Lissage et fermeture ajoutent du
delai, y compris apres la fin d'une fuite. Aucun ajustement sur les tests.

Les huit anciens cas sont des cas de developpement. Neuf autres cas simules
emploient de nouvelles graines, dont une hausse de consommation propre a D
pour montrer une limite. Aucun resultat ne constitue une validation terrain.

Sources :
https://usepa.github.io/EPANET2.2/3_network_model.html
https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.median_abs_deviation.html
https://pandas.pydata.org/docs/reference/api/pandas.Series.rolling.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
from scipy.stats import median_abs_deviation
import wntr

import aqua_sentinel_demo as h
import aqua_detecteur_v1 as v1
import aqua_robustesse_v1 as essais


DOSSIER = Path(__file__).resolve().parent
COLONNES = ["temps_s", "debit_P1_m3_s", "debit_P4_m3_s", "debit_P5_m3_s"]
FENETRE = 5
CONFIRMATION = 3
FERMETURE = 3
CALIBRATION = [dict(id=f"C{i:02d}", diametre_mm=0, facteur_demande=facteur,
                    bruit=True, seed=1000 + i)
               for i, facteur in enumerate([0.75, 0.90, 1.00, 1.10, 1.20, 1.30, 0.65])]
NOUVEAUX = [
    dict(id="T00_normal_085", diametre_mm=0, facteur_demande=0.85, bruit=True, seed=2000),
    dict(id="T01_normal_130", diametre_mm=0, facteur_demande=1.30, bruit=True, seed=2001),
    dict(id="T02_pic_conso", diametre_mm=0, facteur_demande=1.0, bruit=True, seed=2002,
         pic_debut_h=9, pic_fin_h=11, pic_facteur=1.60),
    dict(id="T03_normal_060", diametre_mm=0, facteur_demande=0.60, bruit=True, seed=2003),
    dict(id="T04_fuite_4mm", diametre_mm=4, facteur_demande=1.10, bruit=True, seed=2004),
    dict(id="T05_fuite_6mm", diametre_mm=6, facteur_demande=0.90, bruit=True, seed=2005),
    dict(id="T06_fuite_10mm", diametre_mm=10, facteur_demande=1.20, bruit=True, seed=2006),
    dict(id="T07_fuite_12mm", diametre_mm=12, facteur_demande=0.65, bruit=True, seed=2007),
    dict(id="T08_conso_D_seule", diametre_mm=0, facteur_demande=1.0, bruit=True,
         seed=2008, hors_hypothese=True),
]


def residu(reference, observations):
    v1.verifier_alignement(reference, observations)
    abc_ref = reference["debit_P1_m3_s"] - reference["debit_P4_m3_s"] - reference["debit_P5_m3_s"]
    if (abc_ref <= 1e-9).any():
        raise ValueError("La demande ABC de reference doit etre positive.")
    ratio = (reference["debit_P4_m3_s"] + reference["debit_P5_m3_s"]) / abc_ref
    abc = observations["debit_P1_m3_s"] - observations["debit_P4_m3_s"] - observations["debit_P5_m3_s"]
    return ((observations["debit_P4_m3_s"] + observations["debit_P5_m3_s"]) - ratio * abc) * 1000


def lisser(serie):
    return serie.rolling(FENETRE, min_periods=FENETRE, center=False).mean()


def calibrer(reference, run):
    dossier = run / "calibration_normale"
    dossier.mkdir()
    valeurs = []
    for cas in CALIBRATION:
        print(f"Calibration normale : {cas['id']}", flush=True)
        mesures, _ = essais.simuler(cas)
        chemin = dossier / f"{cas['id']}.csv"
        mesures.to_csv(chemin, index=False)
        observations = v1.lire_csv(chemin, COLONNES)
        valeurs.extend(lisser(residu(reference, observations)).dropna().tolist())
    biais = float(np.median(valeurs))
    sigma = float(median_abs_deviation(valeurs, scale="normal"))
    seuil = max(0.10, 4 * sigma)
    modele = {
        "methode": "bilan_branche_D", "fenetre_mesures": FENETRE,
        "biais_l_s": biais, "sigma_robuste_l_s": sigma,
        "seuil_ouverture_l_s": seuil, "seuil_fermeture_l_s": seuil / 2,
        "confirmation_mesures": CONFIRMATION, "fermeture_mesures": FERMETURE,
        "calibration": "7 cas normaux uniquement, graines 1000 a 1006",
        "hypothese": "demandes proportionnelles, aucune fuite amont, pas de stockage",
        "portee": "branche vers D, pas de localisation complete ni validation terrain",
    }
    v1.ecrire_json(run / "modele_v2.json", modele)
    return modele


def confirmer(scores, modele):
    """Etat causal : confirmation a l'ouverture et a la fermeture."""
    alerte = False
    monte = descend = 0
    resultat = []
    for valeur in scores:
        if not np.isfinite(valeur):
            alerte = False
            monte = descend = 0
        elif alerte:
            descend = descend + 1 if valeur < modele["seuil_fermeture_l_s"] else 0
            if descend >= modele["fermeture_mesures"]:
                alerte = False
                monte = descend = 0
        else:
            monte = monte + 1 if valeur >= modele["seuil_ouverture_l_s"] else 0
            if monte >= modele["confirmation_mesures"]:
                alerte = True
                monte = descend = 0
        resultat.append(int(alerte))
    return resultat


def detecter(reference, observations, modele):
    brut = residu(reference, observations)
    score = lisser(brut) - modele["biais_l_s"]
    return pd.DataFrame({
        "temps_s": observations["temps_s"].to_numpy(),
        "residu_l_s": brut.to_numpy(),
        "score_lisse_l_s": score.fillna(0).to_numpy(),
        "pret": score.notna().astype(int).to_numpy(),
        "alerte": confirmer(score.to_numpy(), modele),
    })


def simuler_hors_hypothese(cas):
    """Cas sans fuite : seule la consommation de D augmente de 50 %."""
    reseau = h.creer_reseau(False)
    for demande in reseau.get_node("D").demand_timeseries_list:
        demande.base_value *= 1.50
    resultat = wntr.sim.WNTRSimulator(reseau).run_sim()
    mesures = resultat.node["pressure"][h.NOEUDS_MESURES].copy()
    mesures.columns = [f"pression_{nom}_m" for nom in h.NOEUDS_MESURES]
    for nom in h.TUYAUX_MESURES:
        mesures[f"debit_{nom}_m3_s"] = resultat.link["flowrate"][nom]
    mesures.index.name = "temps_s"
    if not np.array_equal(mesures.index, np.arange(0, h.DUREE + 1, h.PAS)):
        raise RuntimeError("Calendrier du cas D invalide.")
    if not np.isfinite(mesures.to_numpy()).all():
        raise RuntimeError("Mesures non finies dans le cas D.")
    fuite = resultat.node["leak_demand"]["L4"]
    consommation = resultat.node["demand"][h.NOEUDS_MESURES].sum(axis=1)
    if (resultat.link["flowrate"]["P1"] - consommation - fuite).abs().max() > 1e-6:
        raise RuntimeError("Bilan hydraulique du cas D invalide.")
    if not np.allclose(fuite, 0, atol=1e-10):
        raise RuntimeError("Le cas D ne doit pas contenir de fuite.")
    rng = np.random.default_rng(cas["seed"])
    for nom in mesures.columns:
        sigma = essais.BRUIT_PRESSION_M if nom.startswith("pression_") else essais.BRUIT_DEBIT_M3_S
        mesures[nom] += rng.normal(0, sigma, len(mesures))
    verite = pd.DataFrame({"temps_s": mesures.index, "fuite_active": 0,
                           "debit_fuite_m3_s": 0.0, "tuyau_cible": ""})
    return mesures.reset_index(), verite


def preparer_nouveaux(run):
    dossier = run / "nouveaux"
    (dossier / "observations").mkdir(parents=True)
    (dossier / "verite_evaluation").mkdir()
    for cas in NOUVEAUX:
        print(f"Nouvel essai : {cas['id']}", flush=True)
        mesures, verite = simuler_hors_hypothese(cas) if cas.get("hors_hypothese") else essais.simuler(cas)
        mesures.to_csv(dossier / "observations" / f"{cas['id']}.csv", index=False)
        verite.to_csv(dossier / "verite_evaluation" / f"{cas['id']}.csv", index=False)


def predire(reference, modele, source, cible, cas):
    """Cette fonction ne recoit ni etiquette ni chemin vers la verite."""
    cible.mkdir()
    for configuration in cas:
        nom = configuration["id"] + ".csv"
        observations = v1.lire_csv(source / nom, COLONNES)
        detecter(reference, observations, modele).to_csv(cible / nom, index=False)


def evaluer(source_verite, source_predictions, cas):
    lignes = []
    for configuration in cas:
        nom = configuration["id"] + ".csv"
        prediction = v1.lire_csv(source_predictions / nom, ["temps_s", "alerte"])
        verite = v1.lire_csv(source_verite / nom, ["temps_s", "fuite_active"])
        resultat = v1.evaluer_predictions(prediction, verite)
        evenement = resultat["evenements"][0] if resultat["evenements"] else None
        lignes.append({
            "scenario": configuration["id"],
            "avec_fuite": configuration["diametre_mm"] > 0,
            "hors_hypothese": bool(configuration.get("hors_hypothese", False)),
            "episodes_alerte": v1.compter_episodes(prediction["alerte"]),
            "mesures_fp": resultat["faux_positifs"],
            "mesures_fn": resultat["faux_negatifs"],
            "fuite_detectee": evenement["detectee"] if evenement else None,
            "delai_s": evenement["delai_detection_s"] if evenement else None,
        })
    return lignes


def synthese(lignes):
    sains = [x for x in lignes if not x["avec_fuite"]]
    fuites = [x for x in lignes if x["avec_fuite"]]
    return {
        "cas_sans_fuite": len(sains),
        "cas_sans_fuite_avec_alerte": sum(x["episodes_alerte"] > 0 for x in sains),
        "cas_avec_fuite": len(fuites),
        "cas_avec_fuite_detectee": sum(bool(x["fuite_detectee"]) for x in fuites),
    }


def tracer(ancien, run):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    noms = ["Normal", "Erreurs de mesure", "Consommation +25 %", "Pic de consommation",
            "Fuite 4 mm", "Fuite 8 mm", "Fuite 12 mm avec bruit", "Fuite, consommation -30 %"]
    panneaux = [(ancien / "verite_evaluation", "fuite_active", "Fuites simulees", "#087f8c"),
                (ancien / "predictions", "alerte", "Alertes V1", "#c45531"),
                (run / "predictions_historiques", "alerte", "Alertes V2", "#7860a0")]
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True, sharey=True)
    for ax, (source, colonne, titre, couleur) in zip(axes, panneaux):
        for ligne, cas in enumerate(essais.CAS):
            table = pd.read_csv(source / f"{cas['id']}.csv")
            masque = table[colonne].to_numpy(dtype=bool)
            temps = table["temps_s"].to_numpy()
            debuts = np.flatnonzero(masque & ~np.r_[False, masque[:-1]])
            fins = np.flatnonzero(masque & ~np.r_[masque[1:], False])
            for debut, fin in zip(debuts, fins):
                arret = min(int(temps[fin]) + h.PAS, h.DUREE)
                if arret > temps[debut]:
                    ax.broken_barh([(temps[debut] / 3600, (arret - temps[debut]) / 3600)],
                                   (ligne - 0.3, 0.6), facecolors=couleur)
        ax.set_yticks(range(8), noms)
        ax.set_xlim(0, 24)
        ax.set_xticks(range(0, 25, 2))
        ax.set_title(titre)
        ax.grid(axis="x", alpha=0.2)
        ax.set_axisbelow(True)
    axes[0].invert_yaxis()
    axes[-1].set_xlabel("Temps simule (heures)")
    fig.suptitle("AQUA Sentinel | Reexamen des huit cas de developpement")
    fig.tight_layout()
    fig.savefig(run / "comparaison_v1_v2.png", dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--essais", type=Path, help="Dossier d'une execution de robustesse V1.")
    args = parser.parse_args()
    if args.essais is None:
        racine = DOSSIER / "resultats_aqua" / "robustesse_v1"
        executions = sorted([x for x in racine.iterdir()
                             if (x / "observations" / "N00_temoin.csv").is_file()]) if racine.exists() else []
        if not executions:
            parser.error("Lancer d'abord aqua_robustesse_v1.py.")
        ancien = executions[-1]
    else:
        ancien = args.essais.resolve()
    if (h.PAS, h.DUREE, h.DEBUT_FUITE, h.FIN_FUITE) != (60, 86400, 43200, 64800):
        parser.error("Cette V2 attend le simulateur initial de 24 h, fuite de 12 h a 18 h.")
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = DOSSIER / "resultats_aqua" / "detection_v2" / identifiant
    run.mkdir(parents=True)
    reference = v1.lire_csv(ancien / "observations" / "N00_temoin.csv", COLONNES)
    reference.to_csv(run / "reference.csv", index=False)
    v1.ecrire_json(run / "protocole.json", {
        "calibration": CALIBRATION, "nouveaux_essais": NOUVEAUX,
        "anciens_essais": str(ancien), "fenetre": FENETRE,
        "sha256_v2": essais.empreinte(__file__),
        "sha256_simulateur": essais.empreinte(h.__file__),
        "sha256_generateur_v1": essais.empreinte(essais.__file__),
        "sha256_detecteur_v1": essais.empreinte(v1.__file__),
        "limite": "Cas simules exploratoires, reference ideale, capteurs virtuels.",
    })
    modele = calibrer(reference, run)
    print(f"Seuil V2 fige : {modele['seuil_ouverture_l_s']:.4f} L/s", flush=True)
    modele_hash = essais.empreinte(run / "modele_v2.json")
    predire(reference, modele, ancien / "observations", run / "predictions_historiques", essais.CAS)
    preparer_nouveaux(run)
    predire(reference, modele, run / "nouveaux" / "observations", run / "nouveaux" / "predictions", NOUVEAUX)
    # Toutes les predictions sont sauvegardees avant lecture des etiquettes.
    historique = evaluer(ancien / "verite_evaluation", run / "predictions_historiques", essais.CAS)
    nouveaux = evaluer(run / "nouveaux" / "verite_evaluation", run / "nouveaux" / "predictions", NOUVEAUX)
    baseline = evaluer(ancien / "verite_evaluation", ancien / "predictions", essais.CAS)
    for ligne, ligne_v1 in zip(historique, baseline):
        ligne["episodes_v1"] = ligne_v1["episodes_alerte"]
        ligne["fp_v1"] = ligne_v1["mesures_fp"]
    if essais.empreinte(run / "modele_v2.json") != modele_hash:
        raise RuntimeError("Le modele a change pendant l'evaluation.")
    pd.DataFrame(historique).to_csv(run / "bilan_historique.csv", index=False)
    pd.DataFrame(nouveaux).to_csv(run / "bilan_nouveaux.csv", index=False)
    tracer(ancien, run)
    resume = {
        "origine": "simulation", "verifications_techniques": "PASS",
        "v1_historique": synthese(baseline), "v2_historique": synthese(historique),
        "v2_nouveaux_dans_hypothese": synthese([x for x in nouveaux if not x["hors_hypothese"]]),
        "v2_nouveaux_hors_hypothese": synthese([x for x in nouveaux if x["hors_hypothese"]]),
        "seuils_figes_avant_nouveaux_essais": True,
        "modele": modele, "historique": historique, "nouveaux": nouveaux,
        "limite": "Reference ideale et demandes proportionnelles. Aucune validation terrain.",
    }
    v1.ecrire_json(run / "resume_v2.json", resume)
    print("\nHUIT ANCIENS CAS (episodes V1 puis V2 ; FP = mesures hors fuite)")
    print(pd.DataFrame(historique)[["scenario", "episodes_v1", "episodes_alerte", "mesures_fp", "delai_s"]].to_string(index=False, na_rep="--"))
    print("\nNEUF NOUVEAUX CAS (T08 est volontairement hors hypothese)")
    print(pd.DataFrame(nouveaux)[["scenario", "episodes_alerte", "mesures_fp", "fuite_detectee", "delai_s"]].to_string(index=False, na_rep="--"))
    print("\nLe delai de fermeture peut produire des mesures en alerte apres la fin d'une fuite.")
    print("PASS verifie l'execution technique ; consulter les scores et le cas T08 pour les limites.")
    print(f"Resultats : {run}")


if __name__ == "__main__":
    main()
