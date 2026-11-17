"""AQUA Sentinel : jeu de donnees synthetique pour le premier apprentissage.

Placer a cote de aqua_sentinel_demo.py.
Execution : .venv\\Scripts\\python.exe aqua_dataset_v1.py
Par defaut : 80 scenarios de 24 h, repartis 48/16/16 par scenario entier.
Option : --scenarios 20 pour un petit essai. Le nombre doit etre multiple de 20.
Dependances deja installees : WNTR, numpy et pandas.

Les demandes A/B/C/D ont des profils minute par minute differents, avec des
variations et des pointes legitimes. Les fuites uniques portent sur P2/P3/P4/P5,
avec ouverture de 4/6/8/12 mm, debut de 00 h 30 a 23 h 30 et duree observee
de 30 a 240 minutes. Les evenements sont bornes a la fin de la journee.
Tous les tuyaux candidats sont divises dans tous les cas, y compris sans
fuite. Les capteurs de debit P2/P3/P4/P5 correspondent a leur troncon amont.
Les capteurs virtuels mesurent 4 pressions et 5 debits. Les fluctuations et
bruits sont des hypotheses d'exercice, sans calibration sur un reseau reel.

La topologie reste celle de notre maquette. Ce jeu ne constitue pas une
validation terrain, geographique ou sur un reseau hydraulique jamais vu.
Un scenario de 24 h fournit 1440 lignes sur [0,86400[, sans repeter le point
terminal a 24 h. Les scenarios sont equilibres sain/avec fuite, pas les minutes.

Sorties :
  capteurs/{train,validation,test}.csv : mesures + identifiants de jointure.
  verite/{train,validation,test}.csv : etiquettes reservees a l'apprentissage
    supervise sur train, au reglage sur validation et a l'evaluation sur test.
  protocole.json : parametres physiques, graines et repartition.
  resume_dataset.json et _SUCCESS : controles et empreintes des fichiers.
Ne pas fournir scenario_id, temps_s, protocole ou verite au modele en tant que
variables explicatives. Ne pas melanger les lignes d'un scenario entre jeux.
Les regles et reglages du modele devront etre figes avant evaluation sur test.

Sources :
https://usepa.github.io/WNTR/disaster_models.html
https://usepa.github.io/WNTR/waternetworkmodel.html
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
"""

import argparse
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

import aqua_sentinel_demo as h


DOSSIER = Path(__file__).resolve().parent
PAS = 60
DUREE = 86400
LIGNES = DUREE // PAS
TUYAUX = ["P2", "P3", "P4", "P5"]
OUVERTURES_MM = [4, 6, 8, 12]
CAPTEURS = ([f"pression_{nom}_m" for nom in h.NOEUDS_MESURES]
            + [f"debit_{nom}_m3_s" for nom in h.TUYAUX_MESURES])
COLONNES_CAPTEURS = ["scenario_id", "temps_s"] + CAPTEURS
COLONNES_VERITE = ["scenario_id", "temps_s", "fuite_active", "tuyau_cible", "debit_fuite_m3_s"]
BRUIT_PRESSION_M = 0.03
BRUIT_DEBIT_M3_S = 0.05 / 1000


def ecrire_json(chemin, valeur):
    chemin.write_text(json.dumps(valeur, indent=2, allow_nan=False), encoding="utf-8")


def sha256(chemin):
    calcul = hashlib.sha256()
    with Path(chemin).open("rb") as fichier:
        for bloc in iter(lambda: fichier.read(1024 * 1024), b""):
            calcul.update(bloc)
    return calcul.hexdigest()


def planifier(nombre, graine):
    """Separer les scenarios avant les simulations, avec graines distinctes."""
    repartition = {"train": nombre * 3 // 5, "validation": nombre // 5, "test": nombre // 5}
    graines = np.random.SeedSequence([graine, nombre, 1]).spawn(nombre)
    plan = []
    curseur = 0
    for partie, quantite in repartition.items():
        ordre = np.random.default_rng(graines[curseur]).permutation(quantite)
        # Moitie saine, moitie avec fuite ; ordre de simulation melange.
        for rang in ordre:
            racine = graines[curseur + int(rang)]
            seed = int(racine.generate_state(1, dtype=np.uint64)[0])
            rng = np.random.default_rng(np.random.SeedSequence([seed, 0]))
            avec_fuite = int(rang) >= quantite // 2
            numero_fuite = int(rang) - quantite // 2
            debut = int(rng.integers(30, LIGNES - 29)) * PAS if avec_fuite else None
            duree = int(rng.integers(30, 241)) * PAS if avec_fuite else None
            cible = TUYAUX[numero_fuite % len(TUYAUX)] if avec_fuite else None
            ouverture = OUVERTURES_MM[(numero_fuite // len(TUYAUX) + numero_fuite % len(TUYAUX)) % len(OUVERTURES_MM)] if avec_fuite else 0
            plan.append({
                "scenario_id": "S" + hashlib.sha256(str(seed).encode()).hexdigest()[:12],
                "partie": partie, "seed": seed,
                "avec_fuite": avec_fuite, "tuyau_cible": cible,
                "diametre_fuite_mm": ouverture, "debut_fuite_s": debut,
                "fin_fuite_s": min(debut + duree, DUREE) if avec_fuite else None,
            })
        curseur += quantite
    if len({x["scenario_id"] for x in plan}) != nombre or len({x["seed"] for x in plan}) != nombre:
        raise RuntimeError("Identifiant ou graine duplique.")
    return plan


def construire_reseau(cas):
    reseau = h.creer_reseau(False)
    reseau.options.time.duration = DUREE
    reseau.options.time.hydraulic_timestep = PAS
    reseau.options.time.report_timestep = PAS
    reseau.options.time.pattern_timestep = PAS
    # P4 est deja divise par le simulateur initial. Les autres le sont toujours.
    for tuyau in ["P2", "P3", "P5"]:
        reseau = wntr.morph.split_pipe(reseau, tuyau, tuyau + "_aval", "L" + tuyau[1:], split_at_point=0.5)
    rng = np.random.default_rng(np.random.SeedSequence([cas["seed"], 1]))
    reseau.get_node("R").head_timeseries.base_value = float(rng.uniform(47.0, 53.0))
    base = np.asarray(reseau.get_pattern("jour").multipliers, dtype=float)
    heures = np.arange(LIGNES) / 60
    nominal = np.interp(heures, np.arange(25), np.r_[base, base[0]])
    facteur_global = float(rng.uniform(0.85, 1.15))
    for nom in h.NOEUDS_MESURES:
        facteur_local = float(rng.uniform(0.65, 1.35))
        fluctuation = np.zeros(LIGNES)
        innovations = rng.normal(0, 0.008, LIGNES)
        for i in range(1, LIGNES):
            fluctuation[i] = 0.97 * fluctuation[i - 1] + innovations[i]
        profil = nominal * facteur_global * facteur_local * (1 + fluctuation)
        # Pic legitime propre a ce point, avec debut a une minute quelconque.
        debut = int(rng.integers(30, LIGNES - 29))
        longueur = int(rng.integers(30, 181))
        profil[debut:min(debut + longueur, LIGNES)] *= float(rng.uniform(1.20, 1.70))
        profil = np.clip(profil, 0.10, 3.0)
        nom_pattern = "jour_" + nom
        reseau.add_pattern(nom_pattern, profil)
        reseau.get_node(nom).demand_timeseries_list[0].pattern_name = nom_pattern
    if cas["avec_fuite"]:
        reseau.get_node("L" + cas["tuyau_cible"][1:]).add_leak(
            reseau, area=pi * (cas["diametre_fuite_mm"] / 2000) ** 2,
            discharge_coeff=0.75, start_time=cas["debut_fuite_s"], end_time=cas["fin_fuite_s"],
        )
    return reseau


def simuler(cas):
    reseau = construire_reseau(cas)
    resultat = wntr.sim.WNTRSimulator(reseau).run_sim()
    if not np.array_equal(resultat.node["pressure"].index, np.arange(0, DUREE + 1, PAS)):
        raise RuntimeError("Simulation incomplete : " + cas["scenario_id"])
    fuite_totale = resultat.node["leak_demand"][["L2", "L3", "L4", "L5"]].sum(axis=1)
    consomme = resultat.node["demand"][h.NOEUDS_MESURES].sum(axis=1)
    erreur_bilan = resultat.link["flowrate"]["P1"] - consomme - fuite_totale
    bilan_max = float(erreur_bilan.abs().max())
    if not np.isfinite(bilan_max) or bilan_max > 1e-6:
        raise RuntimeError("Bilan hydraulique invalide : " + cas["scenario_id"])
    pression = resultat.node["pressure"][h.NOEUDS_MESURES]
    if not np.isfinite(pression.to_numpy()).all():
        raise RuntimeError("Pression non finie : " + cas["scenario_id"])
    temps_complets = pression.index.to_numpy()
    actif_complet = np.zeros(len(temps_complets), dtype=bool)
    if cas["avec_fuite"]:
        actif_complet = (temps_complets >= cas["debut_fuite_s"]) & (temps_complets < cas["fin_fuite_s"])
    if not (np.allclose(fuite_totale[~actif_complet], 0, atol=1e-10)
            and (fuite_totale[actif_complet] > 0).all()):
        raise RuntimeError("Calendrier de fuite invalide : " + cas["scenario_id"])
    mesures = pression.iloc[:LIGNES].copy()
    mesures.columns = [f"pression_{nom}_m" for nom in h.NOEUDS_MESURES]
    for nom in h.TUYAUX_MESURES:
        mesures[f"debit_{nom}_m3_s"] = resultat.link["flowrate"][nom].iloc[:LIGNES]
    mesures.index.name = "temps_s"
    if not np.isfinite(mesures.to_numpy()).all():
        raise RuntimeError("Mesure non finie : " + cas["scenario_id"])
    # Une source aleatoire distincte reservee aux erreurs de mesure.
    rng_bruit = np.random.default_rng(np.random.SeedSequence([cas["seed"], 2]))
    for colonne in mesures.columns:
        sigma = BRUIT_PRESSION_M if colonne.startswith("pression_") else BRUIT_DEBIT_M3_S
        mesures[colonne] += rng_bruit.normal(0, sigma, LIGNES)
    mesures = mesures.reset_index()
    mesures.insert(0, "scenario_id", cas["scenario_id"])
    actif = actif_complet[:LIGNES]
    verite = pd.DataFrame({
        "scenario_id": cas["scenario_id"], "temps_s": mesures["temps_s"],
        "fuite_active": actif.astype(int),
        "tuyau_cible": np.where(actif, cas["tuyau_cible"] or "", ""),
        "debit_fuite_m3_s": fuite_totale.iloc[:LIGNES].to_numpy(),
    })
    return mesures[COLONNES_CAPTEURS], verite[COLONNES_VERITE], bilan_max


def verifier_fichiers(run, plan):
    bilan = {}
    ensembles_ids = []
    for partie in ["train", "validation", "test"]:
        mesures = pd.read_csv(run / "capteurs" / (partie + ".csv"))
        verite = pd.read_csv(run / "verite" / (partie + ".csv"), keep_default_na=False)
        if list(mesures.columns) != COLONNES_CAPTEURS or list(verite.columns) != COLONNES_VERITE:
            raise RuntimeError("Schema incorrect : " + partie)
        attendus = {x["scenario_id"] for x in plan if x["partie"] == partie}
        if set(mesures.scenario_id) != attendus or set(verite.scenario_id) != attendus:
            raise RuntimeError("Repartition incorrecte : " + partie)
        if not mesures[["scenario_id", "temps_s"]].equals(verite[["scenario_id", "temps_s"]]):
            raise RuntimeError("Capteurs et verite mal alignes : " + partie)
        if mesures.duplicated(["scenario_id", "temps_s"]).any():
            raise RuntimeError("Cle dupliquee : " + partie)
        if not np.isfinite(mesures[CAPTEURS].to_numpy()).all():
            raise RuntimeError("Capteur absent ou non fini : " + partie)
        if not verite.fuite_active.isin([0, 1]).all():
            raise RuntimeError("Etiquette invalide : " + partie)
        if not np.isfinite(verite.debit_fuite_m3_s.to_numpy()).all():
            raise RuntimeError("Debit de fuite non fini : " + partie)
        for identifiant, groupe in mesures.groupby("scenario_id", sort=False):
            if not np.array_equal(groupe.temps_s, np.arange(LIGNES) * PAS):
                raise RuntimeError("Temps incorrect : " + identifiant)
        for cas in [x for x in plan if x["partie"] == partie]:
            groupe = verite[verite.scenario_id == cas["scenario_id"]]
            attendu = ((groupe.temps_s >= cas["debut_fuite_s"]) & (groupe.temps_s < cas["fin_fuite_s"])) if cas["avec_fuite"] else np.zeros(LIGNES, dtype=bool)
            if not np.array_equal(groupe.fuite_active.to_numpy(dtype=bool), attendu):
                raise RuntimeError("Verite incorrecte : " + cas["scenario_id"])
            if not np.equal(groupe.loc[groupe.fuite_active.eq(0), "tuyau_cible"], "").all():
                raise RuntimeError("Cible presente hors fuite.")
            positifs = groupe[groupe.fuite_active.eq(1)]
            if len(positifs) and (not positifs.tuyau_cible.eq(cas["tuyau_cible"]).all()
                                   or not positifs.debit_fuite_m3_s.gt(0).all()):
                raise RuntimeError("Cible ou debit actif incorrect.")
        ensembles_ids.append(attendus)
        bilan[partie] = {
            "scenarios": len(attendus), "scenarios_avec_fuite": sum(x["avec_fuite"] for x in plan if x["partie"] == partie),
            "lignes": len(mesures), "minutes_avec_fuite": int(verite.fuite_active.sum()),
            "sha256_capteurs": sha256(run / "capteurs" / (partie + ".csv")),
            "sha256_verite": sha256(run / "verite" / (partie + ".csv")),
        }
    if any(ensembles_ids[i] & ensembles_ids[j] for i in range(3) for j in range(i + 1, 3)):
        raise RuntimeError("Un scenario apparait dans plusieurs jeux.")
    return bilan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=int, default=80)
    parser.add_argument("--seed", type=int, default=20261015)
    args = parser.parse_args()
    if args.scenarios < 20 or args.scenarios % 20:
        parser.error("--scenarios doit etre un multiple de 20, au moins 20.")
    if args.seed < 0:
        parser.error("--seed doit etre positif ou nul.")
    plan = planifier(args.scenarios, args.seed)
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = DOSSIER / "data" / "aqua_dataset_v1" / identifiant
    (run / "capteurs").mkdir(parents=True)
    (run / "verite").mkdir()
    ecrire_json(run / "etat.json", {"statut": "EN_COURS"})
    ecrire_json(run / "protocole.json", {
        "origine": "simulation", "seed": args.seed, "scenarios": plan,
        "python": platform.python_version(), "wntr": wntr.__version__,
        "numpy": np.__version__, "pandas": pd.__version__,
        "sha256_generateur": sha256(__file__), "sha256_reseau_initial": sha256(h.__file__),
        "pas_s": PAS, "duree_s": DUREE, "intervalle": "[0,86400[", "lignes_par_scenario": LIGNES,
        "variables_capteurs": CAPTEURS, "jointure_uniquement": ["scenario_id", "temps_s"],
        "unites": {"pression": "m de colonne d'eau", "debit": "m3/s", "temps": "s"},
        "cibles_fuite": TUYAUX, "position_fuite_relative": 0.5,
        "debut_evenement_minute_min_max": [30, LIGNES - 30],
        "bruit_ecart_type_pression_m": BRUIT_PRESSION_M,
        "bruit_ecart_type_debit_l_s": BRUIT_DEBIT_M3_S * 1000,
        "limites": "Maquette unique, parametres pedagogiques, bruit suppose, aucune validation terrain.",
    })
    lots = {partie: {"capteurs": [], "verite": []} for partie in ["train", "validation", "test"]}
    bilan_max = 0.0
    try:
        for numero, cas in enumerate(plan, 1):
            print(f"[{numero}/{len(plan)}] Simulation {cas['partie']} : {cas['scenario_id']}", flush=True)
            mesures, verite, erreur_bilan = simuler(cas)
            bilan_max = max(bilan_max, erreur_bilan)
            partie = cas["partie"]
            if len(mesures) != LIGNES or len(verite) != LIGNES:
                raise RuntimeError("Scenario incomplet avant export : " + cas["scenario_id"])
            lots[partie]["capteurs"].append(mesures)
            lots[partie]["verite"].append(verite)
        # Ecrire chaque fichier complet en une fois, puis le rendre disponible
        # par renommage atomique. Aucun ajout successif a un CSV existant.
        for partie, tables in lots.items():
            for dossier, blocs in tables.items():
                complet = pd.concat(blocs, ignore_index=True)
                attendu = sum(x["partie"] == partie for x in plan) * LIGNES
                if len(complet) != attendu:
                    raise RuntimeError("Lot incomplet avant export : " + partie)
                destination = run / dossier / (partie + ".csv")
                temporaire = destination.with_suffix(".csv.tmp")
                complet.to_csv(temporaire, index=False)
                temporaire.replace(destination)
        bilan = verifier_fichiers(run, plan)
        resume = {
            "origine": "simulation", "verifications": "PASS", "scenarios_total": len(plan),
            "lignes_total": sum(x["lignes"] for x in bilan.values()),
            "lignes_par_scenario": LIGNES, "jeux": bilan,
            "erreur_bilan_max_m3_s": bilan_max, "scenarios_communs_entre_jeux": 0,
            "labels_dans_fichiers_capteurs": False,
            "limite": "Jeu synthetique exploratoire sur une seule maquette ; aucun modele entraine ici.",
        }
        ecrire_json(run / "resume_dataset.json", resume)
        ecrire_json(run / "etat.json", {"statut": "PASS"})
        (run / "_SUCCESS").write_text("PASS\n", encoding="utf-8")
    except Exception as erreur:
        ecrire_json(run / "etat.json", {"statut": "ECHEC", "erreur": str(erreur)})
        raise
    print("\nDATASET AQUA SENTINEL : PASS")
    print("Jeu           Scenarios   Avec fuite   Lignes")
    for partie, information in bilan.items():
        print(f"{partie:12} {information['scenarios']:9} {information['scenarios_avec_fuite']:12} {information['lignes']:8}")
    print(f"Lignes au total : {resume['lignes_total']}")
    print("Scenarios communs entre jeux : 0")
    print("Etiquettes dans les fichiers capteurs : NON")
    print("Ce script prepare les donnees ; aucun modele ML n'est encore entraine.")
    print(f"Resultats : {run}")


if __name__ == "__main__":
    main()
