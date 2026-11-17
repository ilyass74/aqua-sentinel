"""AQUA Sentinel : premier prototype hydraulique autonome.

Reseau propose : R-A, A-B, A-C, B-D, C-D.
Les dimensions et consommations sont des parametres d'exercice.
Les resultats produits sont des simulations, pas des mesures terrain.

Environnement teste : Python 3.12, WNTR 1.5.0.
Dependances : python -m pip install wntr==1.5.0
Execution : python aqua_sentinel_demo.py

References :
https://usepa.github.io/WNTR/waternetworkmodel.html
https://usepa.github.io/WNTR/hydraulics.html
https://usepa.github.io/WNTR/disaster_models.html
https://usepa.github.io/WNTR/units.html
"""

import json
import platform
from math import pi
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # Enregistrer les figures sans ouvrir de fenetre.
import matplotlib.pyplot as plt
import numpy as np
import wntr


DUREE = 24 * 3600         # Secondes de temps simule.
PAS = 60                  # Une mesure par minute.
DEBUT_FUITE = 12 * 3600
FIN_FUITE = 18 * 3600
DIAMETRE_FUITE = 0.012     # Ouverture de 12 mm ; valeur d'exercice.
NOEUDS_MESURES = ["A", "B", "C", "D"]
TUYAUX_MESURES = ["P1", "P2", "P3", "P4", "P5"]
SORTIE = Path(__file__).resolve().parent / "resultats_aqua"


def creer_reseau(avec_fuite=False):
    """Creer un reseau neuf : source, jonctions et tuyaux en boucle."""
    reseau = wntr.network.WaterNetworkModel()
    reseau.options.time.duration = DUREE
    reseau.options.time.hydraulic_timestep = PAS
    reseau.options.time.report_timestep = PAS
    reseau.options.time.pattern_timestep = 3600
    reseau.options.hydraulic.headloss = "H-W"
    reseau.options.hydraulic.demand_model = "PDD"
    reseau.options.hydraulic.minimum_pressure = 5.0   # m de colonne d'eau.
    reseau.options.hydraulic.required_pressure = 25.0
    reseau.options.hydraulic.pressure_exponent = 0.5

    # Multiplicateurs horaires de la consommation, proposes pour l'exercice.
    reseau.add_pattern("jour", [
        .6, .6, .6, .6, .7, .9, 1.2, 1.4, 1.3, 1.1, 1., 1.,
        1.2, 1.1, 1., 1., 1.1, 1.3, 1.4, 1.3, 1.1, .9, .8, .7,
    ])
    # base_head : elevation de la surface d'eau, pas profondeur du reservoir.
    reseau.add_reservoir("R", base_head=50.0, coordinates=(0, 1))

    # nom : (elevation en m, demande en m3/s, position sur le schema).
    jonctions = {
        "A": (10.0, .0010, (1, 1)),
        "B": (11.0, .0015, (2, 2)),
        "C": (12.0, .0012, (2, 0)),
        "D": (10.0, .0010, (3, 1)),
    }
    for nom, (elevation, demande, position) in jonctions.items():
        reseau.add_junction(nom, elevation=elevation, base_demand=demande,
                            demand_pattern="jour", coordinates=position)

    # nom, debut, fin, longueur en m, diametre interieur en m.
    tuyaux = [
        ("P1", "R", "A", 200.0, .15),
        ("P2", "A", "B", 120.0, .10),
        ("P3", "A", "C", 120.0, .10),
        ("P4", "B", "D", 80.0, .10),
        ("P5", "C", "D", 80.0, .10),
    ]
    for nom, debut, fin, longueur, diametre in tuyaux:
        reseau.add_pipe(nom, debut, fin, length=longueur, diameter=diametre,
                       roughness=120.0, initial_status="OPEN")

    # Meme division de P4 dans les deux cas : seule la fuite change.
    # P4 devient B-L4 ; P4_aval devient L4-D. L4 n'est pas un capteur.
    reseau = wntr.morph.split_pipe(reseau, "P4", "P4_aval", "L4",
                                   split_at_point=0.5)
    if avec_fuite:
        reseau.get_node("L4").add_leak(
            reseau, area=pi * (DIAMETRE_FUITE / 2) ** 2,
            discharge_coeff=0.75, start_time=DEBUT_FUITE, end_time=FIN_FUITE,
        )
    return reseau


def simuler(avec_fuite=False):
    """Calculer les pressions/debits et verifier la conservation du debit."""
    reseau = creer_reseau(avec_fuite)
    # Ce moteur prend en charge les fuites ajoutees par add_leak.
    resultat = wntr.sim.WNTRSimulator(reseau).run_sim()
    mesures = resultat.node["pressure"][NOEUDS_MESURES].copy()
    mesures.columns = [f"pression_{nom}_m" for nom in NOEUDS_MESURES]
    for nom in TUYAUX_MESURES:
        mesures[f"debit_{nom}_m3_s"] = resultat.link["flowrate"][nom]
    mesures.index.name = "temps_s"

    # Verifications physiques et temporelles avant export.
    if not np.isfinite(mesures.to_numpy()).all():
        raise RuntimeError("La simulation contient des valeurs non finies.")
    if not np.array_equal(mesures.index, np.arange(0, DUREE + 1, PAS)):
        raise RuntimeError("La simulation est incomplete ou mal horodatee.")
    fuite = resultat.node["leak_demand"]["L4"]
    consommation = resultat.node["demand"][NOEUDS_MESURES].sum(axis=1)
    bilan = resultat.link["flowrate"]["P1"] - consommation - fuite
    if bilan.abs().max() > 1e-6:
        raise RuntimeError("Le bilan des debits n'est pas respecte.")
    return reseau, mesures, fuite


def tracer(reseau, normal, avec_fuite):
    """Dessiner le reseau et comparer les signaux observes."""
    fig, ax = plt.subplots(figsize=(9, 5))
    wntr.graphics.plot_network(reseau, node_labels=False, link_labels=True,
                              node_size=140, link_width=2, ax=ax,
                              show_plot=False, title="Reseau virtuel AQUA Sentinel")
    for nom in reseau.node_name_list:
        etiquette = {"R": "R : source", "L4": "L4 : point de fuite"}.get(nom, nom)
        ax.annotate(etiquette, reseau.get_node(nom).coordinates,
                    xytext=(0, 14), textcoords="offset points", ha="center",
                    fontsize=10, fontweight="bold",
                    bbox={"facecolor": "white", "edgecolor": "none", "alpha": .85})
    ax.set_axis_off()
    fig.savefig(SORTIE / "reseau.png", dpi=160, bbox_inches="tight")
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    heures = normal.index.to_numpy() / 3600
    for donnees, nom, couleur in [(normal, "Normal", "#087f8c"),
                                 (avec_fuite, "Avec fuite", "#d35d3a")]:
        axes[0].plot(heures, donnees["pression_D_m"], label=nom, color=couleur)
        # CSV en m3/s ; affichage du debit d'entree en L/s.
        axes[1].plot(heures, donnees["debit_P1_m3_s"] * 1000,
                     label=nom, color=couleur)
    for ax in axes:
        ax.axvspan(DEBUT_FUITE / 3600, FIN_FUITE / 3600, color="#d35d3a", alpha=.1)
        ax.grid(alpha=.25)
        ax.legend()
    axes[0].set_ylabel("Pression en D (m de colonne d'eau)")
    axes[1].set_ylabel("Debit d'entree P1 (L/s)")
    axes[1].set_xlabel("Temps simule (heures)")
    fig.suptitle("Simulation : fuite sur P4 de 12 h a 18 h")
    fig.tight_layout()
    fig.savefig(SORTIE / "comparaison.png", dpi=160)
    plt.close(fig)


def main():
    SORTIE.mkdir(parents=True, exist_ok=True)
    reseau, normal, fuite_normale = simuler(False)
    _, avec_fuite, debit_fuite = simuler(True)
    actif = (normal.index >= DEBUT_FUITE) & (normal.index < FIN_FUITE)
    # La fuite doit exister seulement pendant l'intervalle configure.
    if not ((debit_fuite[actif] > 0).all()
            and np.allclose(debit_fuite[~actif], 0, atol=1e-10)
            and np.allclose(fuite_normale, 0, atol=1e-10)):
        raise RuntimeError("Le calendrier de fuite est incorrect.")
    # Les deux cas doivent coincider avant et apres la fuite (aucun stockage).
    if not np.allclose(normal.loc[~actif], avec_fuite.loc[~actif], atol=1e-7, rtol=0):
        raise RuntimeError("Une difference apparait hors de l'intervalle de fuite.")

    normal.to_csv(SORTIE / "mesures_normal.csv")
    avec_fuite.to_csv(SORTIE / "mesures_fuite.csv")
    # Verite reservee a l'evaluation : ne pas la fournir au modele de detection.
    verite_dir = SORTIE / "verite_evaluation"
    verite_dir.mkdir(exist_ok=True)
    verite = debit_fuite.rename("debit_fuite_m3_s").to_frame()
    verite.index.name = "temps_s"
    verite["fuite_active"] = actif.astype(int)
    verite["tuyau_cible"] = np.where(actif, "P4", "")
    verite.to_csv(verite_dir / "fuite_P4.csv")
    tracer(reseau, normal, avec_fuite)

    resume = {
        "origine": "simulation", "python": platform.python_version(),
        "wntr": wntr.__version__, "duree_s": DUREE, "pas_s": PAS,
        "lignes_par_scenario": len(normal), "debut_fuite_s": DEBUT_FUITE,
        "fin_fuite_s": FIN_FUITE, "diametre_fuite_m": DIAMETRE_FUITE,
        "pression_D_normale_min_m": float(normal["pression_D_m"].min()),
        "baisse_pression_D_max_m": float((normal["pression_D_m"] - avec_fuite["pression_D_m"]).max()),
        "debit_fuite_max_m3_s": float(debit_fuite.max()),
        "verifications": "PASS",
    }
    (SORTIE / "resume.json").write_text(json.dumps(resume, indent=2), encoding="utf-8")
    print(json.dumps(resume, indent=2))
    print(f"Fichiers produits : {SORTIE}")


if __name__ == "__main__":
    main()
