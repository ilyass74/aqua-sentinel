"""Laboratoire WNTR V2 : deux zones, vannes TCV et regulateur borne.

Reseau EXPERIMENTAL distinct de la maquette V3 a cinq conduites. Les
pressions, elevations, demandes, limites et loi d'ouverture sont des choix
d'exercice. K(u)=2/u**2 est une loi simplifiee non calibree sur une vanne.
Chaque pas est une resolution quasi-statique ; aucun coup de belier ni
transitoire mecanique n'est simule. Aucune commande materielle n'est emise.
"""
from __future__ import annotations
from copy import deepcopy
from functools import lru_cache
from itertools import product
from math import pi, isfinite
import warnings

import wntr

DT = 300
STEPS = 36
MIN_OPENING = .05
MAX_CHANGE = .10
MAX_PRESSURE = 45.
SCENARIOS = {
    "deux_zones": "Deux zones avec des demandes et elevations differentes",
    "pointe_B": "Hausse de consommation en zone B",
    "ressource_limitee": "Charge disponible faible et besoins partiellement servis",
    "capteur_absent": "Pression B indisponible du pas 10 au pas 15",
    "fuite_A": "Fuite sur A ; gel du regulateur sur bilan suspect",
}


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x)


@lru_cache(maxsize=8192)
def _solve(head, da, db, ua, ub, leak_zone, leak_mm, isolate):
    w = wntr.network.WaterNetworkModel()
    w.options.time.duration = 0
    w.options.hydraulic.demand_model = "PDD"
    w.options.hydraulic.headloss = "H-W"
    w.options.hydraulic.minimum_pressure = 5.
    w.options.hydraulic.required_pressure = 25.
    w.options.hydraulic.pressure_exponent = .5
    w.add_reservoir("R", base_head=head, coordinates=(0, 0))
    w.add_junction("T", elevation=0, coordinates=(1, 0))
    w.add_pipe("TRONC", "R", "T", length=600, diameter=.09, roughness=110)
    for z, u, length, elevation, demand in [("A", ua, 100, 0, da), ("B", ub, 600, 10, db)]:
        w.add_junction("I" + z, elevation=elevation, coordinates=(2, 1 if z == "A" else -1))
        w.add_junction("L" + z, elevation=elevation, coordinates=(3, 1 if z == "A" else -1))
        w.add_junction("Z" + z, elevation=elevation, base_demand=demand, coordinates=(4, 1 if z == "A" else -1))
        w.add_valve("V" + z, "T", "I" + z, diameter=.06, valve_type="TCV", initial_setting=2 / u**2)
        for suffix, start, end in [("amont", "I" + z, "L" + z), ("aval", "L" + z, "Z" + z)]:
            w.add_pipe("P" + z + "_" + suffix, start, end, length=length / 2,
                       diameter=.07, roughness=110, initial_status="CLOSED" if z in isolate else "OPEN")
        if z == leak_zone and leak_mm > 0:
            w.get_node("L" + z).add_leak(w, area=pi * (leak_mm / 2000)**2, start_time=0,
                                        discharge_coeff=.75)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        r = wntr.sim.WNTRSimulator(w).run_sim(convergence_error=True)
    zones = {}
    leak_total = 0.
    total_served = 0.
    for z, demand, u in [("A", da, ua), ("B", db, ub)]:
        pressure = float(r.node["pressure"].loc[0, "Z" + z])
        served = float(r.node["demand"].loc[0, "Z" + z])
        leak = float(r.node["leak_demand"].loc[0, "L" + z])
        q_up = float(r.link["flowrate"].loc[0, "PA_amont" if z == "A" else "PB_amont"])
        q_down = float(r.link["flowrate"].loc[0, "PA_aval" if z == "A" else "PB_aval"])
        if not all(isfinite(x) for x in [pressure, served, leak, q_up, q_down]):
            raise ValueError("Solveur hydraulique : resultat non fini.")
        if abs(q_up - q_down - leak) > 1e-6:
            raise ValueError("Bilan de masse du troncon non respecte.")
        zones[z] = {"pression_m": pressure, "demande_l_s": demand * 1000,
            "fourni_l_s": max(0., served) * 1000, "fraction_servie": min(1., max(0., served / demand)),
            "ouverture_pct": u * 100, "coefficient_perte_K": 2 / u**2,
            "ecart_amont_aval_l_s": (q_up - q_down) * 1000, "isolee": z in isolate}
        leak_total += leak
        total_served += max(0., served)
    q_source = float(r.link["flowrate"].loc[0, "TRONC"])
    if abs(q_source - total_served - leak_total) > 2e-6:
        raise ValueError("Conservation de la masse globale non respectee.")
    return {"zones": zones, "debit_source_l_s": q_source * 1000,
        "debit_fuite_simule_l_s": max(0., leak_total * 1000),
        "fraction_min_servie": min(z["fraction_servie"] for z in zones.values()),
        "fourni_total_l_s": total_served * 1000}


def solve(head=55., da=.006, db=.004, ua=1., ub=1., leak_zone="", leak_mm=0., isolate=()):
    values = [head, da, db, ua, ub, leak_mm]
    if not all(finite(x) for x in values) or not 15 <= head <= 90 or not .001 <= da <= .015 or not .001 <= db <= .015:
        raise ValueError("Parametres hors limites de la maquette.")
    if not MIN_OPENING <= ua <= 1 or not MIN_OPENING <= ub <= 1 or not 0 <= leak_mm <= 12:
        raise ValueError("Ouverture ou diametre de fuite hors limites.")
    if leak_zone not in ("", "A", "B") or set(isolate) - {"A", "B"}:
        raise ValueError("Zone inconnue.")
    return deepcopy(_solve(*[round(float(x), 8) for x in [head, da, db, ua, ub]], leak_zone,
                           round(float(leak_mm), 8), tuple(sorted(isolate))))


def objective(result, previous, candidate):
    fractions = [z["fraction_servie"] for z in result["zones"].values()]
    excess = sum(max(0., z["pression_m"] - MAX_PRESSURE)**2 for z in result["zones"].values())
    movement = sum(abs(a - b) for a, b in zip(previous, candidate))
    # Max-min de service d'abord ; deficit total ensuite. Penalites explicites.
    return 10 * min(fractions) + sum(fractions) - .002 * excess - .002 * movement


def choose(head, da, db, previous, observation, quality=True):
    if not quality:
        return previous, "gel_donnees_insuffisantes"
    if any(z["ecart_amont_aval_l_s"] > .15 for z in observation["zones"].values()):
        return previous, "gel_bilan_suspect_fuite_ou_biais"
    candidates = []
    # Chercher une cible au-dela du prochain petit mouvement : une recherche
    # purement locale reste bloquee quand plusieurs positions donnent le meme
    # debit (plateau de demande PDD satisfaite). Seule la premiere variation
    # bornee vers cette cible est appliquee, puis la decision est recalculee.
    choices = [tuple(k / 20 for k in range(1, 21))] * 2
    for settings in product(*choices):
        forecast = solve(head, da, db, *settings)  # modele nominal, sans fuite connue du futur
        if any(z["pression_m"] < 0 for z in forecast["zones"].values()):
            continue
        candidates.append((objective(forecast, previous, settings), -sum(abs(a-b) for a,b in zip(previous, settings)), settings))
    if not candidates:
        return previous, "gel_aucun_reglage_admissible"
    target = max(candidates)[2]
    best = tuple(round(u + max(-MAX_CHANGE, min(MAX_CHANGE, v-u)), 4) for u,v in zip(previous,target))
    return best, "maintien" if best == previous else "ajustement_borne"


def conditions(scenario, index):
    head = 70. if scenario == "deux_zones" else (30. if scenario == "ressource_limitee" else 55.)
    da, db = .006, .004
    if scenario == "pointe_B" and 10 <= index < 24:
        db = .0065
    leak_mm = 10. if scenario == "fuite_A" and 10 <= index < 24 else 0.
    quality = not (scenario == "capteur_absent" and 10 <= index < 16)
    return head, da, db, leak_mm, quality


def regulation(scenario="pointe_B"):
    if scenario not in SCENARIOS:
        raise ValueError("Scenario de regulation inconnu.")
    settings = (1., 1.)
    records = []
    for i in range(STEPS):
        head, da, db, leak_mm, quality = conditions(scenario, i)
        before_decision = solve(head, da, db, *settings, leak_zone="A", leak_mm=leak_mm)
        new_settings, reason = choose(head, da, db, settings, before_decision, quality)
        if any(abs(a-b) > MAX_CHANGE + 1e-8 for a,b in zip(settings, new_settings)):
            raise ValueError("Vitesse de variation de consigne depassee.")
        baseline = solve(head, da, db, 1., 1., leak_zone="A", leak_mm=leak_mm)
        controlled = solve(head, da, db, *new_settings, leak_zone="A", leak_mm=leak_mm)
        records.append({"temps_s": i * DT, "charge_source_m": head, "qualite_mesures": quality,
            "sans_regulation": baseline, "avec_regulation": controlled, "decision": reason,
            "variation_max_pct": max(abs(a-b) for a,b in zip(settings,new_settings)) * 100})
        settings = new_settings
    def metrics(name):
        values = [r[name] for r in records]
        return {"fraction_min_moyenne": sum(r["fraction_min_servie"] for r in values) / len(values),
            "fraction_min_pire": min(r["fraction_min_servie"] for r in values),
            "volume_fourni_m3": sum(r["fourni_total_l_s"] * DT / 1000 for r in values),
            "volume_fuite_simule_m3": sum(r["debit_fuite_simule_l_s"] * DT / 1000 for r in values),
            "minutes_zone_sous_95pct": sum(z["fraction_servie"] < .95 for r in values for z in r["zones"].values()) * DT / 60,
            "pression_max_m": max(z["pression_m"] for r in values for z in r["zones"].values()),
            "pression_min_m": min(z["pression_m"] for r in values for z in r["zones"].values())}
    return {"origine": "simulation_hydraulique_V2", "scenario": scenario, "libelle": SCENARIOS[scenario],
        "reseau": "laboratoire deux zones distinct du reseau V3", "pas_s": DT, "duree_s": DT * STEPS,
        "controleur": {"type": "recherche d'une cible sur grille puis mouvement borne, non LLM et non modele entraine",
            "objectif": "10*minimum(fractions_servies)+somme(fractions_servies)-0.002*surpression_au_carre-0.002*mouvement",
            "pression_max_objectif_m": MAX_PRESSURE, "variation_max_par_pas_pct": MAX_CHANGE * 100,
            "limite_pression": "Penalite de surpression, pas garantie de respect si le reseau ne le permet pas."},
        "series": records, "sans_regulation": metrics("sans_regulation"), "avec_regulation": metrics("avec_regulation"),
        "limite": "Besoin de chaque zone connu du modele ; choix max-min peut reduire la desserte d'une zone pour aider l'autre. Loi K simplifiee non calibree ; etats quasi-statiques sans transitoires ni coup de belier. Aucun gain garanti sur un reseau reel. Aucun ordre materiel."}


def interventions(zone="A", ouverture_mm=8., duree_min=30):
    if zone not in ("A", "B") or not finite(ouverture_mm) or not 4 <= ouverture_mm <= 12 or isinstance(duree_min,bool) or not isinstance(duree_min,int) or not 5 <= duree_min <= 120:
        raise ValueError("Zone A/B, ouverture 4 a 12 mm et duree entiere 5 a 120 min requises.")
    other = "B" if zone == "A" else "A"
    actions = [("surveiller", "Conserver les vannes ouvertes", (), (1.,1.)),
        ("reduire", "Reduire l'ouverture du cote signale", (), (.2,1.) if zone=="A" else (1.,.2)),
        ("isoler_cible", "Isoler le troncon signale par ses deux extremites", (zone,), (1.,1.)),
        ("isoler_autre", "Isoler l'autre zone pour comparaison", (other,), (1.,1.))]
    rows=[]
    for action,label,isolate,settings in actions:
        result=solve(55.,.006,.004,*settings,leak_zone=zone,leak_mm=float(ouverture_mm),isolate=isolate)
        rows.append({"action":action,"libelle":label,"zones_isolees":list(isolate),
            "volume_fuite_simule_m3":result["debit_fuite_simule_l_s"]*duree_min*60/1000,
            "volume_fourni_simule_m3":result["fourni_total_l_s"]*duree_min*60/1000,**result})
    return {"origine":"simulation_hydraulique_V2","zone_signalee":zone,"ouverture_fuite_mm":ouverture_mm,
        "horizon_min":duree_min,"alternatives":rows,
        "limite":"Contexte de fuite fourni par l'utilisateur pour une experience. L'isolement ferme les deux extremites du troncon. Volumes extrapoles a debit constant sur l'horizon, hors transitoires. Ce n'est ni une instruction terrain ni une localisation supplementaire du detecteur V3."}
