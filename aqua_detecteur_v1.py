"""AQUA Sentinel : premiere regle de detection, sur simulation connue.

Depuis le dossier aqua-sentinel :
  .venv\\Scripts\\python.exe aqua_detecteur_v1.py detecter
  .venv\\Scripts\\python.exe aqua_detecteur_v1.py evaluer

Dependances deja installees : numpy, pandas, matplotlib.
La detection ne lit jamais le fichier de verite. La reference normale est
une simulation sans fuite aux memes instants, avec les memes consommations.
Les seuils sont des choix pedagogiques pour cet exercice, pas des seuils
valides sur un reseau reel ou un jeu de test independant.

Unites WNTR : https://usepa.github.io/WNTR/units.html
Evaluation : https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


SEUIL_SURDEBIT_L_S = 0.50
SEUIL_BAISSE_PRESSION_M = 0.10
PERSISTANCE_MESURES = 3
PAS_S = 60
COLONNES = ["temps_s", "pression_D_m", "debit_P1_m3_s"]


def lire_csv(chemin, colonnes):
    """Refuser les series manquantes ou mal alignees plutot que les combler."""
    donnees = pd.read_csv(chemin, usecols=colonnes)[colonnes]
    donnees = donnees.apply(pd.to_numeric, errors="raise")
    if len(donnees) < PERSISTANCE_MESURES:
        raise ValueError(f"Serie trop courte : {chemin}")
    if not np.isfinite(donnees.to_numpy(dtype=float)).all():
        raise ValueError(f"Valeur absente ou non finie : {chemin}")
    temps = donnees["temps_s"].to_numpy(dtype=float)
    if (temps < 0).any() or not np.equal(temps, np.floor(temps)).all():
        raise ValueError(f"Les temps doivent etre des secondes entieres : {chemin}")
    if not np.equal(np.diff(temps), PAS_S).all():
        raise ValueError(f"Il faut des temps croissants espaces de {PAS_S} s : {chemin}")
    donnees["temps_s"] = donnees["temps_s"].astype("int64")
    return donnees


def verifier_alignement(reference, observations):
    if not np.array_equal(reference["temps_s"], observations["temps_s"]):
        raise ValueError("Les fichiers doivent couvrir exactement les memes instants.")


def detecter(reference, observations):
    """Regle causale : utiliser seulement le present et les mesures passees."""
    verifier_alignement(reference, observations)
    surdebit = (observations["debit_P1_m3_s"] - reference["debit_P1_m3_s"]) * 1000
    baisse = reference["pression_D_m"] - observations["pression_D_m"]
    anomalie = (surdebit >= SEUIL_SURDEBIT_L_S) & (baisse >= SEUIL_BAISSE_PRESSION_M)
    compteur = 0
    alertes = []
    for valeur in anomalie:
        compteur = compteur + 1 if valeur else 0
        alertes.append(int(compteur >= PERSISTANCE_MESURES))
    # La confirmation reste a son instant reel ; aucune alerte n'est antidatee.
    return pd.DataFrame({
        "temps_s": observations["temps_s"].to_numpy(),
        "surdebit_P1_l_s": surdebit.to_numpy(),
        "baisse_pression_D_m": baisse.to_numpy(),
        "anomalie_brute": anomalie.astype(int).to_numpy(),
        "alerte": alertes,
    })


def compter_episodes(masque):
    masque = np.asarray(masque, dtype=bool)
    return int(np.count_nonzero(masque & ~np.r_[False, masque[:-1]]))


def ecrire_json(chemin, contenu):
    chemin.write_text(json.dumps(contenu, indent=2, allow_nan=False), encoding="utf-8")


def tracer(detection, chemin):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    heures = detection["temps_s"].to_numpy() / 3600
    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
    axes[0].plot(heures, detection["surdebit_P1_l_s"], color="#087f8c")
    axes[0].axhline(SEUIL_SURDEBIT_L_S, color="#ba4a2a", linestyle="--", label="Seuil")
    axes[0].set_ylabel("Surdebit P1 (L/s)")
    axes[1].plot(heures, detection["baisse_pression_D_m"], color="#087f8c")
    axes[1].axhline(SEUIL_BAISSE_PRESSION_M, color="#ba4a2a", linestyle="--", label="Seuil")
    axes[1].set_ylabel("Baisse en D (m d'eau)")
    axes[2].step(heures, detection["anomalie_brute"], where="post", linestyle="--",
                 color="#89939b", label="Anomalie brute")
    axes[2].step(heures, detection["alerte"], where="post", color="#ba4a2a",
                 label="Alerte confirmee")
    axes[2].set_ylabel("Alerte (0 ou 1)")
    axes[2].set_yticks([0, 1])
    axes[2].set_ylim(-0.1, 1.15)
    axes[2].set_xlabel("Temps simule (heures)")
    for ax in axes:
        ax.grid(alpha=0.2)
        ax.legend(loc="upper right")
    fig.suptitle("AQUA Sentinel | Premiere detection par comparaison au normal")
    fig.tight_layout()
    fig.savefig(chemin, dpi=160)
    plt.close(fig)


def lancer_detection(donnees, sortie):
    reference = lire_csv(donnees / "mesures_normal.csv", COLONNES)
    observations = lire_csv(donnees / "mesures_fuite.csv", COLONNES)
    normal = detecter(reference, reference)
    fuite = detecter(reference, observations)
    sortie.mkdir(parents=True, exist_ok=True)
    normal.to_csv(sortie / "normal_alertes.csv", index=False)
    fuite.to_csv(sortie / "fuite_alertes.csv", index=False)
    tracer(fuite, sortie / "detection.png")
    instants = fuite.loc[fuite["alerte"] == 1, "temps_s"]
    resume = {
        "methode": "regle_sur_reference_normale_simulee",
        "seuil_surdebit_l_s": SEUIL_SURDEBIT_L_S,
        "seuil_baisse_pression_m": SEUIL_BAISSE_PRESSION_M,
        "persistance_mesures": PERSISTANCE_MESURES,
        "pas_s": PAS_S,
        "verite_utilisee_pour_detecter": False,
        "lignes": len(fuite),
        "episodes_alerte_normal": compter_episodes(normal["alerte"]),
        "episodes_alerte_scenario": compter_episodes(fuite["alerte"]),
        "premiere_alerte_s": int(instants.iloc[0]) if len(instants) else None,
        "limite": "Scenario connu, reference parfaite ; aucune validation terrain.",
    }
    ecrire_json(sortie / "detection_resume.json", resume)
    print(json.dumps(resume, indent=2))
    print(f"Sorties : {sortie}")


def evaluer_predictions(predictions, verite):
    """Comparer les sorties figees aux etiquettes ; ne jamais retoucher la regle."""
    verifier_alignement(predictions, verite)
    for tableau, nom in [(predictions, "alerte"), (verite, "fuite_active")]:
        if not tableau[nom].isin([0, 1]).all():
            raise ValueError(f"{nom} doit contenir uniquement 0 ou 1.")
    predit = predictions["alerte"].to_numpy(dtype=bool)
    vrai = verite["fuite_active"].to_numpy(dtype=bool)
    temps = predictions["temps_s"].to_numpy(dtype="int64")
    vp = int(np.count_nonzero(predit & vrai))
    fp = int(np.count_nonzero(predit & ~vrai))
    fn = int(np.count_nonzero(~predit & vrai))
    vn = int(np.count_nonzero(~predit & ~vrai))
    precision = vp / (vp + fp) if vp + fp else None
    rappel = vp / (vp + fn) if vp + fn else None
    debuts = np.flatnonzero(vrai & ~np.r_[False, vrai[:-1]])
    fins = np.flatnonzero(vrai & ~np.r_[vrai[1:], False]) + 1
    evenements = []
    for debut, fin in zip(debuts, fins):
        # Une alerte deja ouverte avant la fuite ne compte pas comme sa detection.
        nouvelles = predit & ~np.r_[False, predit[:-1]]
        confirmees = np.flatnonzero(nouvelles[debut:fin]) + debut
        premier = int(temps[confirmees[0]]) if len(confirmees) else None
        evenements.append({
            "debut_fuite_s": int(temps[debut]),
            "premiere_alerte_s": premier,
            "delai_detection_s": premier - int(temps[debut]) if premier is not None else None,
            "detectee": premier is not None,
        })
    return {
        "evaluation": "scenario_simule_connu_uniquement",
        "unite_matrice": "mesure, pas evenement",
        "vrais_positifs": vp, "faux_positifs": fp,
        "faux_negatifs": fn, "vrais_negatifs": vn,
        "precision": precision, "rappel": rappel,
        "episodes_alerte_hors_fuite": compter_episodes(predit & ~vrai),
        "evenements": evenements,
        "limite": "Pas de jeu de test independant, pas de bruit ni de derive de consommation.",
    }


def lancer_evaluation(donnees, sortie):
    # Cette commande ne recalcule aucune prediction et ne modifie aucun seuil.
    predictions = lire_csv(sortie / "fuite_alertes.csv", ["temps_s", "alerte"])
    verite = lire_csv(donnees / "verite_evaluation" / "fuite_P4.csv",
                      ["temps_s", "fuite_active"])
    resultat = evaluer_predictions(predictions, verite)
    ecrire_json(sortie / "evaluation.json", resultat)
    print(json.dumps(resultat, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["detecter", "evaluer"])
    parser.add_argument("--donnees", type=Path,
                        default=Path(__file__).resolve().parent / "resultats_aqua")
    parser.add_argument("--sortie", type=Path)
    args = parser.parse_args()
    sortie = args.sortie if args.sortie is not None else args.donnees / "detection_v1"
    try:
        if args.action == "detecter":
            lancer_detection(args.donnees, sortie)
        else:
            lancer_evaluation(args.donnees, sortie)
    except (OSError, ValueError) as erreur:
        parser.exit(1, f"Erreur AQUA : {erreur}\n")


if __name__ == "__main__":
    main()
