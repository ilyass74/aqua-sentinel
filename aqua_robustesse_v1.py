"""AQUA Sentinel : huit essais de resistance de notre premier detecteur.

Placer ce fichier a cote de aqua_sentinel_demo.py et aqua_detecteur_v1.py.
Execution : .venv\\Scripts\\python.exe aqua_robustesse_v1.py
Dependances : celles du projet ; aucune installation supplementaire.

Les seuils du detecteur restent fixes. Les cas ci-dessous sont exploratoires,
pas un jeu de test terrain ni une estimation de performance en production.
Le bruit est gaussien independant, choisi pour l'exercice : ecart-type de
0.03 m sur chaque pression et 0.05 L/s sur chaque debit. Aucune specification
de capteur reel ne justifie ces valeurs. La reference est une simulation
ideale de la consommation nominale, jamais ajustee aux cas observes.

Sources :
https://usepa.github.io/WNTR/disaster_models.html
https://usepa.github.io/WNTR/units.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
"""

import hashlib
import json
import platform
from datetime import datetime, timezone
from math import pi
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import wntr

import aqua_sentinel_demo as hydraulique
import aqua_detecteur_v1 as detecteur


BRUIT_PRESSION_M = 0.03
BRUIT_DEBIT_M3_S = 0.05 / 1000
CAS = [
    dict(id="N00_temoin", diametre_mm=0, facteur_demande=1.0, bruit=False, seed=100),
    dict(id="N01_bruit", diametre_mm=0, facteur_demande=1.0, bruit=True, seed=101),
    dict(id="N02_conso_plus25", diametre_mm=0, facteur_demande=1.25, bruit=False, seed=102),
    dict(id="N03_pic_conso", diametre_mm=0, facteur_demande=1.0, bruit=False, seed=103,
         pic_debut_h=14, pic_fin_h=16, pic_facteur=1.40),
    dict(id="F04_fuite_4mm", diametre_mm=4, facteur_demande=1.0, bruit=False, seed=104),
    dict(id="F05_fuite_8mm", diametre_mm=8, facteur_demande=1.0, bruit=False, seed=105),
    dict(id="F06_fuite_12mm_bruit", diametre_mm=12, facteur_demande=1.0, bruit=True, seed=106),
    dict(id="F07_fuite_conso_moins30", diametre_mm=12, facteur_demande=0.70, bruit=True, seed=107),
]
DOSSIER = Path(__file__).resolve().parent


def empreinte(chemin):
    return hashlib.sha256(Path(chemin).read_bytes()).hexdigest()


def simuler(cas):
    reseau = hydraulique.creer_reseau(False)
    for nom in hydraulique.NOEUDS_MESURES:
        for demande in reseau.get_node(nom).demand_timeseries_list:
            demande.base_value *= cas["facteur_demande"]
    if "pic_debut_h" in cas:
        pattern = reseau.get_pattern("jour")
        multiplicateurs = np.asarray(pattern.multipliers, dtype=float).copy()
        multiplicateurs[cas["pic_debut_h"]:cas["pic_fin_h"]] *= cas["pic_facteur"]
        pattern.multipliers = multiplicateurs
    if cas["diametre_mm"] > 0:
        reseau.get_node("L4").add_leak(
            reseau, area=pi * (cas["diametre_mm"] / 2000) ** 2,
            discharge_coeff=0.75, start_time=hydraulique.DEBUT_FUITE,
            end_time=hydraulique.FIN_FUITE,
        )
    resultat = wntr.sim.WNTRSimulator(reseau).run_sim()
    mesures = resultat.node["pressure"][hydraulique.NOEUDS_MESURES].copy()
    mesures.columns = [f"pression_{nom}_m" for nom in hydraulique.NOEUDS_MESURES]
    for nom in hydraulique.TUYAUX_MESURES:
        mesures[f"debit_{nom}_m3_s"] = resultat.link["flowrate"][nom]
    mesures.index.name = "temps_s"
    if not np.array_equal(mesures.index, np.arange(0, hydraulique.DUREE + 1, hydraulique.PAS)):
        raise RuntimeError(f"Calendrier hydraulique invalide : {cas['id']}")
    if not np.isfinite(mesures.to_numpy()).all():
        raise RuntimeError(f"Valeur hydraulique non finie : {cas['id']}")
    fuite = resultat.node["leak_demand"]["L4"]
    consommation = resultat.node["demand"][hydraulique.NOEUDS_MESURES].sum(axis=1)
    bilan = resultat.link["flowrate"]["P1"] - consommation - fuite
    if bilan.abs().max() > 1e-6:
        raise RuntimeError(f"Bilan hydraulique invalide : {cas['id']}")
    actif = ((mesures.index >= hydraulique.DEBUT_FUITE)
             & (mesures.index < hydraulique.FIN_FUITE)
             & (cas["diametre_mm"] > 0))
    if not (np.allclose(fuite[~actif], 0, atol=1e-10)
            and (fuite[actif] > 0).all()):
        raise RuntimeError(f"Calendrier de fuite invalide : {cas['id']}")
    verite = pd.DataFrame({
        "temps_s": mesures.index,
        "fuite_active": actif.astype(int),
        "debit_fuite_m3_s": fuite.to_numpy(),
        "tuyau_cible": np.where(actif, "P4", ""),
    })
    # Le bilan physique est controle AVANT l'ajout d'erreurs de mesure.
    if cas["bruit"]:
        rng = np.random.default_rng(cas["seed"])
        for nom in mesures.columns:
            sigma = BRUIT_PRESSION_M if nom.startswith("pression_") else BRUIT_DEBIT_M3_S
            mesures[nom] += rng.normal(0.0, sigma, size=len(mesures))
    return mesures.reset_index(), verite


def preparer_essais(run):
    """Generer les observations et leur verite dans des dossiers distincts."""
    observations_dir = run / "observations"
    verite_dir = run / "verite_evaluation"
    observations_dir.mkdir()
    verite_dir.mkdir()
    for cas in CAS:
        print(f"Simulation : {cas['id']}", flush=True)
        mesures, verite = simuler(cas)
        mesures.to_csv(observations_dir / f"{cas['id']}.csv", index=False)
        verite.to_csv(verite_dir / f"{cas['id']}.csv", index=False)
    # Le temoin exact fournit une reference de calcul, pas un test independant.
    reference = detecteur.lire_csv(observations_dir / "N00_temoin.csv", detecteur.COLONNES)
    reference.to_csv(run / "reference_nominale.csv", index=False)


def predire_essais(run):
    """Lire seulement la reference et les capteurs, jamais la verite."""
    reference = detecteur.lire_csv(run / "reference_nominale.csv", detecteur.COLONNES)
    predictions_dir = run / "predictions"
    predictions_dir.mkdir()
    for cas in CAS:
        observations = detecteur.lire_csv(run / "observations" / f"{cas['id']}.csv", detecteur.COLONNES)
        predictions = detecteur.detecter(reference, observations)
        predictions.to_csv(predictions_dir / f"{cas['id']}.csv", index=False)


def evaluer_essais(run):
    """Evaluer apres sauvegarde de toutes les predictions."""
    lignes = []
    for cas in CAS:
        predictions = detecteur.lire_csv(run / "predictions" / f"{cas['id']}.csv", ["temps_s", "alerte"])
        verite_complete = pd.read_csv(run / "verite_evaluation" / f"{cas['id']}.csv")
        verite = detecteur.lire_csv(run / "verite_evaluation" / f"{cas['id']}.csv", ["temps_s", "fuite_active"])
        resultat = detecteur.evaluer_predictions(predictions, verite)
        evenement = resultat["evenements"][0] if resultat["evenements"] else None
        lignes.append({
            "scenario": cas["id"],
            "avec_fuite": cas["diametre_mm"] > 0,
            "diametre_fuite_mm": cas["diametre_mm"],
            "debit_fuite_max_l_s": float(verite_complete["debit_fuite_m3_s"].max() * 1000),
            "episodes_alerte": detecteur.compter_episodes(predictions["alerte"]),
            "mesures_faux_positifs": resultat["faux_positifs"],
            "mesures_faux_negatifs": resultat["faux_negatifs"],
            "precision_mesures": resultat["precision"],
            "rappel_mesures": resultat["rappel"],
            "fuite_detectee": evenement["detectee"] if evenement is not None else None,
            "delai_detection_s": evenement["delai_detection_s"] if evenement is not None else None,
        })
    return lignes


def tracer_bilan(run):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, sharey=True)
    sources = [("verite_evaluation", "fuite_active", "Fuites simulees", "#087f8c"),
               ("predictions", "alerte", "Alertes du detecteur", "#c45531")]
    for ax, (dossier, colonne, titre, couleur) in zip(axes, sources):
        for ligne, cas in enumerate(CAS):
            donnees = pd.read_csv(run / dossier / f"{cas['id']}.csv")
            masque = donnees[colonne].to_numpy(dtype=bool)
            temps = donnees["temps_s"].to_numpy()
            debuts = np.flatnonzero(masque & ~np.r_[False, masque[:-1]])
            fins = np.flatnonzero(masque & ~np.r_[masque[1:], False])
            for debut, fin in zip(debuts, fins):
                arret = min(int(temps[fin]) + hydraulique.PAS, hydraulique.DUREE)
                if arret > temps[debut]:
                    ax.broken_barh([(temps[debut] / 3600, (arret - temps[debut]) / 3600)],
                                   (ligne - 0.3, 0.6), facecolors=couleur)
        ax.set_title(titre)
        ax.set_yticks(range(len(CAS)), [cas["id"] for cas in CAS])
        ax.set_xlim(0, 24)
        ax.set_xticks(range(0, 25, 2))
        ax.grid(axis="x", alpha=0.25)
        ax.set_axisbelow(True)
    axes[0].invert_yaxis()
    axes[1].set_xlabel("Temps simule (heures)")
    fig.suptitle("AQUA Sentinel | Essais exploratoires, seuils fixes")
    fig.tight_layout()
    fig.savefig(run / "bilan_scenarios.png", dpi=160)
    plt.close(fig)


def main():
    if (detecteur.SEUIL_SURDEBIT_L_S, detecteur.SEUIL_BAISSE_PRESSION_M,
            detecteur.PERSISTANCE_MESURES, detecteur.PAS_S) != (0.5, 0.1, 3, 60):
        raise RuntimeError("Ces essais attendent le detecteur V1 avec ses seuils d'origine.")
    if (hydraulique.PAS, hydraulique.DUREE) != (60, 86400):
        raise RuntimeError("Ces essais attendent le simulateur initial : 24 h, pas de 60 s.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = DOSSIER / "resultats_aqua" / "robustesse_v1" / run_id
    run.mkdir(parents=True)
    manifeste = {
        "origine": "simulation", "run_id": run_id,
        "python": platform.python_version(), "wntr": wntr.__version__,
        "numpy": np.__version__, "pandas": pd.__version__,
        "cas": CAS,
        "bruit_ecart_type_pression_m": BRUIT_PRESSION_M,
        "bruit_ecart_type_debit_l_s": BRUIT_DEBIT_M3_S * 1000,
        "reference": "consommation_nominale_sans_bruit_sans_fuite",
        "seuil_surdebit_l_s": detecteur.SEUIL_SURDEBIT_L_S,
        "seuil_baisse_pression_m": detecteur.SEUIL_BAISSE_PRESSION_M,
        "persistance_mesures": detecteur.PERSISTANCE_MESURES,
        "sha256_simulateur": empreinte(hydraulique.__file__),
        "sha256_detecteur": empreinte(detecteur.__file__),
        "sha256_essais": empreinte(__file__),
        "usage": "Exploration uniquement, sans ajustement automatique des seuils.",
    }
    detecteur.ecrire_json(run / "manifeste.json", manifeste)
    preparer_essais(run)
    predire_essais(run)
    # La verite est exploitee pour les scores seulement apres les predictions.
    lignes = evaluer_essais(run)
    pd.DataFrame(lignes).to_csv(run / "bilan_scenarios.csv", index=False)
    tracer_bilan(run)
    sains = [ligne for ligne in lignes if not ligne["avec_fuite"]]
    fuites = [ligne for ligne in lignes if ligne["avec_fuite"]]
    resume = {
        "origine": "simulation", "scenarios_total": len(lignes),
        "scenarios_sans_fuite": len(sains),
        "scenarios_sans_fuite_avec_alerte": sum(ligne["episodes_alerte"] > 0 for ligne in sains),
        "scenarios_avec_fuite": len(fuites),
        "scenarios_avec_fuite_detectee": sum(bool(ligne["fuite_detectee"]) for ligne in fuites),
        "seuils_modifies": False,
        "verifications_techniques": "PASS",
        "limite": "8 cas exploratoires, reference ideale, bruit suppose ; aucune validation terrain.",
        "resultats": lignes,
    }
    detecteur.ecrire_json(run / "resume_robustesse.json", resume)
    print("\nBILAN DES ESSAIS (FP = mesures en alerte hors fuite)")
    tableau = pd.DataFrame(lignes)[["scenario", "episodes_alerte", "mesures_faux_positifs",
                                    "fuite_detectee", "delai_detection_s"]]
    print(tableau.to_string(index=False, na_rep="--"))
    print(f"\nVerifications techniques : PASS. Cas sains avec alerte : {resume['scenarios_sans_fuite_avec_alerte']}/{len(sains)}.")
    print(f"Cas avec fuite detectee : {resume['scenarios_avec_fuite_detectee']}/{len(fuites)}.")
    print("PASS porte sur les calculs et fichiers ; il ne signifie pas que toutes les fuites sont detectees.")
    print(f"Resultats : {run}")


if __name__ == "__main__":
    main()
