"""Supervision V3. Calculs explicatifs experimentaux, distincts du detecteur fige.

Les references par heure sont ajustees sur les seuls scenarios SANS fuite de
train. Aucun test ou etiquette courante ne sert au calcul des preuves.
Les quatre secteurs pilotes sont les intervalles instrumentes P2--P5 ; ce
ne sont pas quatre DMA de terrain certifiees. Acoustique et niveau restent
indisponibles tant qu'une mesure supplementaire n'a pas ete fournie.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
import csv
import hashlib
import json
import math
from statistics import median

from aqua_v2_core import PIPES, FLOWS, OPTIONAL, number

ENDS = {"P2": ("A", "B"), "P3": ("A", "C"), "P4": ("B", "D"), "P5": ("C", "D")}
WEIGHTS = {"debit": 30, "pression": 30, "acoustique": 20, "persistance": 15, "qualite": 5}
LIMIT = ("Indice de preuves experimental, non calibre comme probabilite de fuite. "
         "La gravite est separee de cet indice. Biais, fuite et soutirage inconnu "
         "peuvent produire le meme bilan. Les references historiques n'ont pas de calendrier hebdomadaire.")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def stats(values, floor):
    center = median(values)
    return {"centre": center, "dispersion": max(floor, 1.4826 * median(abs(v-center) for v in values)), "n": len(values)}


class Reference:
    def __init__(self, base):
        self.profile = {}
        self.info = {"disponible": False, "raison": "Fichiers train absents ; aucune baseline inventee."}
        from pathlib import Path
        directory = Path(base.meta_detection["dataset_original"])
        sensors, truth = directory / "capteurs/train.csv", directory / "verite/train.csv"
        if not sensors.is_file() or not truth.is_file():
            return
        healthy = set()
        leaking = set()
        with truth.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                healthy.add(r["scenario_id"])
                if int(r["fuite_active"]):
                    leaking.add(r["scenario_id"])
        healthy -= leaking
        groups = defaultdict(list)
        with sensors.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r["scenario_id"] not in healthy:
                    continue
                bucket = int(r["temps_s"]) // 900
                for p in PIPES:
                    groups[(p, bucket, "debit")].append(1000 * float(r[f"debit_{p}_m3_s"]))
                    groups[(p, bucket, "pression")].append(float(r[f"pression_{ENDS[p][1]}_m"]))
        self.profile = {k: stats(v, .03 if k[2] == "debit" else .05) for k, v in groups.items()}
        self.info = {"disponible": bool(groups), "jeu": "train uniquement, scenarios sans fuite",
                     "scenarios": len(healthy), "classes_horaires": 96, "hebdomadaire_disponible": False,
                     "sha256_capteurs": hashlib.sha256(sensors.read_bytes()).hexdigest(),
                     "sha256_verite_train": hashlib.sha256(truth.read_bytes()).hexdigest(),
                     "regle": "Mediane et MAD par quart d'heure ; seuils explicatifs a 3 dispersions, non valides sur terrain."}
        self.info["version"] = digest(self.info)

    def get(self, pipe, t, signal):
        return self.profile.get((pipe, int(t) // 900, signal))

    def export(self, pipe):
        return [{"temps_s": k*900, "debit_l_s": self.get(pipe, k*900, "debit"),
                 "pression_m": self.get(pipe, k*900, "pression")} for k in range(96)]


def raw_rows(store, sid):
    store.session(sid)
    with store.connection() as c:
        rows = c.execute("SELECT sequence,entree_json,sortie_json FROM mesures WHERE session_id=? ORDER BY sequence", (sid,)).fetchall()
    return [{"sequence": r[0], "entree": json.loads(r[1]), "sortie": json.loads(r[2])} for r in rows]


def sensor_health(rows, reference, complements):
    if not rows:
        return []
    last = rows[-1]
    signals = last["sortie"]["qualite"]
    results = []
    for name in FLOWS + OPTIONAL:
        q = signals[name]
        flags = [q["raison"]] if q["raison"] else []
        # Une tendance du residu ou un ecart de voisinage ne diagnostique PAS
        # un capteur : une modification hydraulique peut donner ce signal.
        if name.startswith("debit_P") and "aval" not in name and name[6:8] in PIPES:
            p = name[6:8]
            window = rows[-12:]
            residuals = []
            for r in window:
                v = r["entree"]["mesure"].get(name)
                ref = reference.get(p, r["sortie"]["temps_s"], "debit")
                if number(v) and ref and r["sortie"]["qualite"][name]["etat"] == "utilisable":
                    residuals.append((1000*v-ref["centre"])/ref["dispersion"])
            if len(residuals) == 12 and all(b > a for a,b in zip(residuals, residuals[1:])) and residuals[-1]-residuals[0] >= 3:
                flags.append("derive_ou_evolution_hydraulique_a_verifier")
            pair = last["sortie"]["paires"][p]
            if pair["niveau_seuil"] is not None and abs(pair["niveau_seuil"]) >= 1:
                flags.append("divergence_amont_aval_fuite_ou_biais")
        results.append({"capteur": name, "etat": "suspect" if flags else q["etat"], "raisons": flags,
                        "qualite_utilisation_0_100": 0 if q["etat"] != "utilisable" else (50 if flags else 100),
                        "interpretation": "Indice d'utilisabilite a regles, pas probabilite de bon fonctionnement.",
                        "source": f"mesures:{last['sortie']['session_id']}:{last['sequence']}"})
    # Les valeurs supplementaires ne sont jamais assimilees a des mesures V3.
    ext = complements.get(str(last["sequence"]), {})
    for p in PIPES:
        value = ext.get("acoustique_db", {}).get(p)
        results.append({"capteur": "acoustique_"+p, "etat": "disponible" if value is not None else "non_fourni",
                        "raisons": [], "qualite_utilisation_0_100": 100 if value is not None else None,
                        "source": f"complement:{last['sequence']}" if value is not None else None})
    return results


def analyze(store, reference, sid, complements=None, weekly=None):
    session = store.session(sid)
    rows = raw_rows(store, sid)
    complements, weekly = complements or {}, weekly or {}
    result = {"session_id": sid, "origine": "simulation", "scenario_id": session["scenario_id"],
              "sequence": session["sequence"], "automatique": bool(session["automatique"]),
              "temps_s": rows[-1]["sortie"]["temps_s"] if rows else None,
              "reference": reference.info, "secteurs": [], "capteurs": [],
              "limite": LIMIT, "interpretation_secteurs": "4 secteurs pilotes entre paires ; aucune DMA de terrain certifiee."}
    if not rows:
        result["kpi"] = {"etat": "sans_mesure", "sante_surveillee_pct": None, "alertes_actives": 0,
                         "volume_estime_m3": None, "pression_moyenne_m": None, "capteurs_suspects": 0, "critiques": 0}
        return result
    last = rows[-1]
    out, measure = last["sortie"], last["entree"]["mesure"]
    extension = complements.get(str(last["sequence"]), {})
    for pipe in PIPES:
        name = f"debit_{pipe}_m3_s"
        pn = f"pression_{ENDS[pipe][1]}_m"
        pair = out["paires"][pipe]
        flow = 1000 * measure[name] if number(measure.get(name)) and out["qualite"][name]["etat"] == "utilisable" else None
        pressure = measure.get(pn) if out["qualite"][pn]["etat"] == "utilisable" else None
        fq, pr = reference.get(pipe, out["temps_s"], "debit"), reference.get(pipe, out["temps_s"], "pression")
        flow_z = max(0., (flow-fq["centre"])/fq["dispersion"]) if flow is not None and fq else None
        press_z = max(0., (pr["centre"]-pressure)/pr["dispersion"]) if pressure is not None and pr else None
        aq = extension.get("acoustique_db", {}).get(pipe)
        ar = extension.get("reference_acoustique_db", {}).get(pipe)
        acoustic = max(0., (aq-ar)/10) if aq is not None and ar is not None else None
        streak = 0
        night_streak = 0
        recent = []
        valid_points, alert_points, pressures = 0, 0, []
        first_alert = None
        previous_t = None
        for row in rows:
            t = row["sortie"]["temps_s"]
            if previous_t is not None and t-previous_t != 60:
                streak = night_streak = 0
            x = row["sortie"]["paires"][pipe]
            valid = x["etat"] not in ("donnees_insuffisantes", "initialisation")
            if valid:
                valid_points += 1
                alert_points += int(x["alerte_memorisee"])
            if x["alerte_memorisee"] and first_alert is None:
                first_alert = t
            pv = row["entree"]["mesure"].get(pn)
            if number(pv) and row["sortie"]["qualite"][pn]["etat"] == "utilisable":
                pressures.append(pv)
            streak = streak+1 if valid and x["niveau_seuil"] is not None and x["niveau_seuil"] >= 1 else 0
            raw = row["entree"]["mesure"].get(name)
            r = reference.get(pipe, t, "debit")
            night = 3600 <= t < 18000  # 01:00--05:00, temps relatif de cette simulation
            good = number(raw) and row["sortie"]["qualite"][name]["etat"] == "utilisable"
            anomalous = bool(night and good and r and 1000*raw > r["centre"]+3*r["dispersion"])
            night_streak = night_streak+1 if anomalous else 0
            if night and good:
                recent.append((t, 1000*raw, r["centre"] if r else None))
            previous_t = t
        valid_pair = pair["etat"] not in ("donnees_insuffisantes", "initialisation")
        ratio = pair["niveau_seuil"] if valid_pair else None
        # Le bilan appaire reste la principale preuve ; un simple changement
        # de consommation ne devient pas une fuite pour le detecteur fige.
        evidence = {"debit": min(1., max(0., ratio or 0.)) if ratio is not None else None,
                    "pression": min(1., press_z/3) if press_z is not None else None,
                    "acoustique": min(1., acoustic) if acoustic is not None else None,
                    "persistance": min(1., streak/5) if valid_pair else None,
                    "qualite": 1. if valid_pair and pressure is not None else (0. if not valid_pair else .5)}
        points = sum(WEIGHTS[k]*v for k,v in evidence.items() if v is not None)
        available = sum(WEIGHTS[k] for k,v in evidence.items() if v is not None)
        critical = bool(pressure is not None and (pressure < 10 or pressure > 60))
        high = bool(valid_pair and pair["alerte_memorisee"] and (pair["ecart_corrige_l_s"] or 0) >= 1.)
        state = "indetermine" if not valid_pair else ("alerte" if pair["alerte_memorisee"] else ("surveiller" if night_streak >= 5 else "surveillance"))
        recommendation = ("Verifier les capteurs avant de conclure." if not valid_pair else
                          "Inspecter le troncon entre les deux debitmetres ; comparer fuite, biais et soutirage." if pair["alerte_memorisee"] else
                          "Verifier la consommation nocturne et les soutirages declares." if night_streak >= 5 else "Poursuivre la surveillance.")
        result["secteurs"].append({"id": pipe, "nom": "Secteur pilote "+pipe, "noeuds": list(ENDS[pipe]),
            "etat": state, "alerte_detecteur_fige": pair["alerte_memorisee"], "gravite": "critique" if critical else ("elevee" if high else "ordinaire"),
            "gravite_regle": "Critique si pression disponible <10 ou >60 mCE ; elevee si alerte et bilan >=1 L/s. Limites d'exercice.",
            "pression_m": pressure, "debit_entree_l_s": flow, "bilan_l_s": pair["ecart_corrige_l_s"],
            "volume_estime_m3": pair["volume_non_comptabilise_estime_m3"], "duree_integree_s": pair["duree_integree_s"],
            "intervalles_exclus": pair["intervalles_exclus"], "score_preuves_0_100": round(points, 1),
            "couverture_preuves_sur_100": available, "poids": WEIGHTS, "preuves_normalisees": evidence,
            "probabilite_fuite": None, "debit_ecart_reference_z": flow_z, "baisse_pression_z": press_z,
            "baseline_debit": fq, "baseline_pression": pr,
            "pression_min_recue_m": min(pressures, default=None), "pression_max_recue_m": max(pressures, default=None),
            "taux_mesures_alerte_sur_valides": alert_points/valid_points if valid_points else None,
            "mesures_valides": valid_points, "premier_signal_recu_s": first_alert,
            "localisation": f"Intervalle instrumente {pipe}, de {ENDS[pipe][0]} vers {ENDS[pipe][1]} ; pas une coordonnee de trou.",
            "cause": pair["cause"], "recommandation": recommendation,
            "nocturne": {"fenetre": "01:00--05:00, horloge relative de simulation", "mesures_disponibles": len(recent),
                "minimum_observe_l_s": min((v[1] for v in recent), default=None),
                "minimum_reference_sur_instants_observes_l_s": min((v[2] for v in recent if v[2] is not None), default=None),
                "minutes_consecutives_anormales": night_streak, "alerte": night_streak >= 5,
                "nuit_complete": len(recent) == 240 and len({x[0] for x in recent}) == 240,
                "interpretation": "Minimum sur les seuls points recus ; une consommation legitime peut aussi augmenter ce debit."},
            "source": f"mesures:{sid}:{last['sequence']}:{pipe}"})
    result["capteurs"] = sensor_health(rows, reference, complements)
    valid = [s for s in result["secteurs"] if s["etat"] != "indetermine"]
    alerted = [s for s in result["secteurs"] if s["alerte_detecteur_fige"]]
    observed_pressures = [measure[f"pression_{n}_m"] for n in "ABCD" if out["qualite"][f"pression_{n}_m"]["etat"] == "utilisable"]
    result["kpi"] = {"etat": "alerte" if alerted else ("indetermine" if len(valid) < 4 else "surveillance"),
        "sante_surveillee_pct": round(100*sum(s["etat"] == "surveillance" for s in valid)/len(valid), 1) if valid else None,
        "couverture_secteurs": len(valid), "alertes_actives": len(alerted),
        "volume_estime_m3": sum(s["volume_estime_m3"] for s in result["secteurs"]),
        "pression_moyenne_m": sum(observed_pressures)/len(observed_pressures) if observed_pressures else None,
        "capteurs_suspects": sum(s["etat"] == "suspect" for s in result["capteurs"]),
        "critiques": sum(s["gravite"] == "critique" for s in result["secteurs"]),
        "interpretation_sante": "Part des secteurs utilisables sans alerte ni anomalie nocturne ; pas une sante physique mesuree."}
    result["equipements"] = {"niveau_reservoir_m": extension.get("niveau_reservoir_m"),
                              "etat_pompe": extension.get("etat_pompe", "non_fourni"),
                              "origine": "complement declare par l'operateur, simulation"}
    result["priorites"] = [s["id"] for s in sorted(result["secteurs"], key=lambda s: (s["gravite"] == "critique", s["volume_estime_m3"]), reverse=True)]
    result["hebdomadaire"] = weekly or {"disponible": False, "raison": "Pas de dates dans les scenarios historiques. Importer des references datees explicites."}
    result["tendances"] = [{"temps_s": r["sortie"]["temps_s"], **{p: r["sortie"]["paires"][p]["ecart_corrige_l_s"] for p in PIPES}} for r in rows[-180:]]
    return result


def weekly_profile(samples):
    """References datees explicitement fournies ; jamais fabriquees du test."""
    groups = defaultdict(list)
    days = defaultdict(set)
    for s in samples:
        dt = datetime.fromisoformat(s["horodatage"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Horodatage avec fuseau requis.")
        # L'heure et le jour sont ceux du decalage fourni ; ne pas melanger
        # des fuseaux differents dans le meme lot de reference.
        key = (s["conduite"], dt.weekday(), dt.hour)
        groups[key].append(s)
        days[key].add(dt.date().isoformat())
    offsets = {datetime.fromisoformat(s["horodatage"].replace("Z", "+00:00")).utcoffset().total_seconds() for s in samples}
    if len(offsets) != 1:
        raise ValueError("Un seul decalage horaire est requis par lot.")
    return {"disponible": True, "origine": "references de simulation declarees, pas validation terrain", "version": digest(samples),
        "decalage_utc_s": next(iter(offsets)), "points": len(samples),
        "classes": [{"conduite": k[0], "jour_semaine": k[1], "heure": k[2], "jours_distincts": len(days[k]),
                     "debit_l_s": stats([s["debit_l_s"] for s in values], .03),
                     "pression_m": stats([s["pression_m"] for s in values], .05)} for k, values in sorted(groups.items())],
        "limite": "Profil descriptif du lot de reference importe ; classe absente = inconnue. Aucune date n'est ajoutee aux anciens tests."}
