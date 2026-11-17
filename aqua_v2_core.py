"""Etat causal, controle des mesures, estimation des bilans et journal SQLite.

Module V2 experimental. Les seuils du detecteur V3 restent ceux du modele
fige. Le controle qualite ne permet pas de distinguer un biais de debitmetre
d'une fuite produisant le meme bilan. Les volumes sont des bilans estimes,
pas une mesure certaine de fuite. Aucun fichier de verite n'est utilise.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import combinations
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

PIPES = ("P2", "P3", "P4", "P5")
FLOWS = tuple(f"debit_{p}{s}_m3_s" for p in PIPES for s in ("", "_aval"))
OPTIONAL = ("debit_P1_m3_s",) + tuple(f"pression_{p}_m" for p in "ABCD")
WORKFLOW = ("nouveau", "a_verifier", "inspection_planifiee", "en_intervention", "resolu")


def utc():
    return datetime.now(timezone.utc).isoformat()


def dumps(x):
    return json.dumps(x, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def number(x):
    return isinstance(x, (float, int)) and not isinstance(x, bool) and math.isfinite(x)


def initial_pipe():
    return {"raw": [], "scores": [], "active": False, "on": 0, "off": 0,
            "incident": None, "reprise": False, "reprise_compteur": 0, "precedent": None,
            "bilan_l": 0., "marge_l": 0., "duree_valide_s": 0, "intervalles_exclus": 0}


def initial_state():
    return {"temps_s": None, "capteurs": {}, "paires": {p: initial_pipe() for p in PIPES}}


def validate_measure(measure, scenario_id):
    if not isinstance(measure, dict) or set(measure) - set(FLOWS + OPTIONAL + ("scenario_id", "temps_s")):
        raise ValueError("Champs inconnus ou colonnes de verite dans les mesures.")
    if measure.get("scenario_id") != scenario_id:
        raise ValueError("Scenario different de celui de la session.")
    t = measure.get("temps_s")
    if isinstance(t, bool) or not isinstance(t, int) or not 0 <= t <= 86340 or t % 60:
        raise ValueError("temps_s doit etre un entier au pas de 60 s entre 0 et 86340.")
    # Absences et null sont des observations de mauvaise qualite, pas des zeros.
    for k, v in measure.items():
        if k in FLOWS + OPTIONAL and v is not None and not number(v):
            raise ValueError("Valeur non numerique ou non finie : " + k)


def causal_step(state, measure, calibration, corrections=None, horodatages=None):
    """Un point seulement ; aucune consultation du futur ni d'etiquette.

    Corrections = soutirage legitime + variation de stockage connus, en L/s,
    entre les deux metres. Le reseau historique les suppose nuls.
    La marge cumulee est une enveloppe indicative 3*sigma, pas un IC calibre.
    """
    corrections = corrections or {}
    horodatages = horodatages or {}
    if set(corrections) - set(PIPES) or any(not number(v) for v in corrections.values()):
        raise ValueError("Corrections invalides ; cles P2 a P5 et valeurs finies en L/s.")
    if set(horodatages) - set(FLOWS + OPTIONAL) or any(isinstance(v, bool) or not isinstance(v, int) for v in horodatages.values()):
        raise ValueError("Horodatages invalides.")
    t = measure["temps_s"]
    previous_t = state["temps_s"]
    if previous_t is not None and t <= previous_t:
        raise ValueError("Horodatage ancien ou duplique.")
    gap = previous_t is not None and t - previous_t != 60
    quality = {}
    for name in FLOWS + OPTIONAL:
        v = measure.get(name)
        history = state["capteurs"].setdefault(name, [])
        reason = None
        if v is None:
            reason = "absent"
        elif name in horodatages and horodatages[name] != t:
            reason = "desynchronise"
        elif (name.startswith("debit") and abs(v) > .1) or (name.startswith("pression") and not -5 <= v <= 150):
            reason = "hors_plage_exercice"
        if gap:
            history.clear()
        if reason is None:
            history.append(float(v))
            del history[:-6]
            if len(history) == 6 and max(history) - min(history) <= 1e-12:
                reason = "valeur_figee_suspecte"
        else:
            history.clear()
        quality[name] = {"etat": "suspect" if reason else "utilisable", "raison": reason}
    outputs = {}
    events = []
    for pipe in PIPES:
        s = state["paires"][pipe]
        c = calibration[pipe]
        valid = not gap and all(quality[f"debit_{pipe}{k}_m3_s"]["etat"] == "utilisable" for k in ("", "_aval"))
        was_active = s["active"]
        if not valid:
            s["raw"], s["scores"] = [], []
            s["on"] = s["off"] = 0
            s["reprise"] = True
            s["reprise_compteur"] = 0
            s["precedent"] = None
            if previous_t is not None:
                s["intervalles_exclus"] += 1
            status = "donnees_insuffisantes"
            level = residual = None
        else:
            s["reprise_compteur"] += 1
            residual = 1000 * (measure[f"debit_{pipe}_m3_s"] - measure[f"debit_{pipe}_aval_m3_s"]) - corrections.get(pipe, 0) - c["centre_l_s"]
            if s["precedent"] is not None:
                s["bilan_l"] += .5 * (s["precedent"] + residual) * 60
                s["marge_l"] += 3 * math.sqrt(3) * c["dispersion_l_s"] * 60
                s["duree_valide_s"] += 60
            elif previous_t is not None:
                s["intervalles_exclus"] += 1
            s["precedent"] = residual
            s["raw"].append(residual)
            del s["raw"][:-3]
            score = sum(s["raw"]) / 3 if len(s["raw"]) == 3 else 0.
            s["scores"].append(score)
            del s["scores"][:-3]
            smooth = sum(s["scores"]) / len(s["scores"])
            level = smooth / c["seuil_l_s"]
            ready = len(s["raw"]) == 3 and (not s["reprise"] or s["reprise_compteur"] >= 5)
            if ready:
                if s["active"]:
                    s["off"] = s["off"] + 1 if smooth < c["seuil_l_s"] / 2 else 0
                    if s["off"] >= 3:
                        s["active"], s["on"], s["off"] = False, 0, 0
                else:
                    s["on"] = s["on"] + 1 if smooth >= c["seuil_l_s"] else 0
                    if s["on"] >= 3:
                        s["active"], s["on"], s["off"] = True, 0, 0
            status = "alerte" if s["active"] else ("pas_de_declenchement" if ready else "initialisation")
            if not ready and s["reprise"]:
                status = "donnees_insuffisantes"
            if ready:
                s["reprise"] = False
        if s["active"] and not was_active:
            events.append({"action": "ouvrir", "conduite": pipe, "temps_s": t})
        if was_active and not s["active"]:
            events.append({"action": "terminer_signal", "conduite": pipe, "temps_s": t})
        outputs[pipe] = {"etat": status, "alerte_memorisee": bool(s["active"]), "niveau_seuil": level,
            "ecart_corrige_l_s": residual, "bilan_signe_m3": s["bilan_l"] / 1000,
            "volume_non_comptabilise_estime_m3": max(0., s["bilan_l"]) / 1000,
            "enveloppe_indicative_m3": [max(0., s["bilan_l"] - s["marge_l"]) / 1000,
                                        max(0., s["bilan_l"] + s["marge_l"]) / 1000],
            "duree_integree_s": s["duree_valide_s"], "intervalles_exclus": s["intervalles_exclus"],
            "cause": "fuite_ou_biais_ou_soutirage_non_modele_non_distinguables_par_le_bilan_seul" if s["active"] else None}
    state["temps_s"] = t
    uncertain = any(x["etat"] == "donnees_insuffisantes" for x in outputs.values())
    active = [p for p in PIPES if outputs[p]["etat"] == "alerte"]
    initializing = any(x["etat"] == "initialisation" for x in outputs.values())
    return {"temps_s": t, "origine": "simulation", "qualite": quality, "paires": outputs,
        "etat_global": "alerte" if active else ("donnees_insuffisantes" if uncertain else ("initialisation" if initializing else "surveillance")),
        "conduites_signalees": active, "trou_temporel": gap,
        "volume_interpretation": "Bilan trapezoidal sur intervalles valides seulement ; soutirages/stockages corriges si connus. Biais capteur et fuite ne sont pas identifiables separement. Enveloppe indicative, pas intervalle de confiance."}, events


class Store:
    def __init__(self, path, calibration):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.calibration = calibration
        self.lock = threading.RLock()
        with self.connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS meta(cle TEXT PRIMARY KEY,valeur TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,scenario_id TEXT NOT NULL,
                  debut_index INTEGER NOT NULL,sequence INTEGER NOT NULL DEFAULT 0,automatique INTEGER NOT NULL DEFAULT 0,
                  config_json TEXT NOT NULL,etat_json TEXT NOT NULL,cree_utc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mesures(session_id TEXT NOT NULL,sequence INTEGER NOT NULL,
                  empreinte TEXT NOT NULL,temps_s INTEGER NOT NULL,entree_json TEXT NOT NULL,sortie_json TEXT NOT NULL,
                  PRIMARY KEY(session_id,sequence),FOREIGN KEY(session_id) REFERENCES sessions(id));
                CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,conduite TEXT NOT NULL,
                  debut_s INTEGER NOT NULL,fin_s INTEGER,etat_signal TEXT NOT NULL,statut TEXT NOT NULL,
                  cree_utc TEXT NOT NULL,FOREIGN KEY(session_id) REFERENCES sessions(id));
                CREATE TABLE IF NOT EXISTS journal(id TEXT PRIMARY KEY,incident_id TEXT NOT NULL,
                  action TEXT NOT NULL,note TEXT NOT NULL,cree_utc TEXT NOT NULL,cle TEXT UNIQUE NOT NULL,
                  FOREIGN KEY(incident_id) REFERENCES incidents(id));
                CREATE TABLE IF NOT EXISTS experiences(id TEXT PRIMARY KEY,type TEXT NOT NULL,
                  parametres_json TEXT NOT NULL,resultat_json TEXT NOT NULL,cree_utc TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS incidents_session ON incidents(session_id);
            """)
            signature = hashlib.sha256(dumps(calibration).encode()).hexdigest()
            old = c.execute("SELECT valeur FROM meta WHERE cle='calibration'").fetchone()
            if old and old[0] != signature:
                raise ValueError("Calibration differente de celle de cette base ; utiliser un autre fichier SQLite.")
            c.execute("INSERT OR IGNORE INTO meta VALUES('calibration',?)", (signature,))
            c.execute("INSERT OR IGNORE INTO meta VALUES('schema','2.0')")

    @contextmanager
    def connection(self):
        with self.lock:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            try:
                c.execute("PRAGMA foreign_keys=ON")
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def create(self, scenario_id, debut_index=0, config=None):
        sid = uuid4().hex
        with self.connection() as c:
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)",
                (sid, scenario_id, debut_index, 0, 0, dumps(config or {}), dumps(initial_state()), utc()))
        return self.session(sid)

    def session(self, sid):
        with self.connection() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise KeyError("Session inconnue.")
            result = dict(row)
            result["configuration"] = json.loads(result.pop("config_json"))
            result.pop("etat_json")
            last = c.execute("SELECT sortie_json FROM mesures WHERE session_id=? ORDER BY sequence DESC LIMIT 1", (sid,)).fetchone()
            result["dernier"] = json.loads(last[0]) if last else None
            return result

    def sessions(self):
        with self.connection() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM sessions ORDER BY cree_utc DESC LIMIT 100")]
        return [self.session(s) for s in ids]

    def autoplay(self, sid, active):
        with self.connection() as c:
            if c.execute("UPDATE sessions SET automatique=? WHERE id=?", (int(active), sid)).rowcount != 1:
                raise KeyError("Session inconnue.")
        return self.session(sid)

    def ingest(self, sid, sequence, measure, corrections=None, horodatages=None):
        envelope = {"mesure": measure, "corrections_l_s": corrections or {}, "horodatages": horodatages or {}}
        encoded = dumps(envelope)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connection() as c:
            session = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if session is None:
                raise KeyError("Session inconnue.")
            validate_measure(measure, session["scenario_id"])
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError("Sequence invalide.")
            old = c.execute("SELECT empreinte,sortie_json FROM mesures WHERE session_id=? AND sequence=?", (sid, sequence)).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError("Conflit : meme sequence et contenu different.")
                return dict(json.loads(old[1]), requete_rejouee=True)
            if sequence != session["sequence"]:
                raise ValueError("Sequence attendue : " + str(session["sequence"]))
            state = json.loads(session["etat_json"])
            result, events = causal_step(state, measure, self.calibration, corrections, horodatages)
            for e in events:
                pair = state["paires"][e["conduite"]]
                if e["action"] == "ouvrir":
                    incident = uuid4().hex
                    pair["incident"] = incident
                    c.execute("INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)", (incident, sid, e["conduite"], e["temps_s"], None, "actif", "nouveau", utc()))
                else:
                    if pair["incident"]:
                        c.execute("UPDATE incidents SET fin_s=?,etat_signal='termine' WHERE id=?", (e["temps_s"], pair["incident"]))
                        self._journal(c, pair["incident"], "signal_termine", "Fin du signal du detecteur ; ne prouve pas une reparation.", "auto_fin:" + pair["incident"])
                        pair["incident"] = None
            for p in PIPES:
                incident = state["paires"][p]["incident"]
                if incident:
                    signal = "incertain_qualite" if result["paires"][p]["etat"] == "donnees_insuffisantes" else "actif"
                    c.execute("UPDATE incidents SET etat_signal=? WHERE id=?", (signal, incident))
                    self._journal(c, incident, "declenchement", "Desequilibre persistant entre les mesures de debit.", "auto_debut:" + incident)
            result.update(session_id=sid, sequence=sequence, requete_rejouee=False)
            c.execute("INSERT INTO mesures VALUES(?,?,?,?,?,?)", (sid, sequence, fingerprint, measure["temps_s"], encoded, dumps(result)))
            c.execute("UPDATE sessions SET sequence=?,etat_json=? WHERE id=?", (sequence + 1, dumps(state), sid))
            return result

    def history(self, sid, limit=180):
        self.session(sid)
        with self.connection() as c:
            rows = c.execute("SELECT sortie_json FROM mesures WHERE session_id=? ORDER BY sequence DESC LIMIT ?", (sid, limit)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def _journal(self, c, incident, action, note, key):
        c.execute("INSERT OR IGNORE INTO journal VALUES(?,?,?,?,?,?)", (uuid4().hex, incident, action, note, utc(), key))

    def incidents(self, session_id=None):
        with self.connection() as c:
            rows = c.execute("SELECT * FROM incidents" + (" WHERE session_id=?" if session_id else "") + " ORDER BY cree_utc DESC LIMIT 500", (session_id,) if session_id else ()).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["journal"] = [dict(r) for r in c.execute("SELECT action,note,cree_utc FROM journal WHERE incident_id=? ORDER BY rowid", (row["id"],))]
                results.append(item)
        return results

    def annotate(self, incident, status, note, key):
        if status not in WORKFLOW or not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
            raise ValueError("Statut connu et commentaire de 1 a 1000 caracteres requis.")
        if not isinstance(key, str) or not 8 <= len(key) <= 100:
            raise ValueError("Cle d'idempotence de 8 a 100 caracteres requise.")
        with self.connection() as c:
            item = c.execute("SELECT * FROM incidents WHERE id=?", (incident,)).fetchone()
            if item is None:
                raise KeyError("Incident inconnu.")
            old = c.execute("SELECT incident_id,action,note FROM journal WHERE cle=?", (key,)).fetchone()
            if old:
                if (old[0], old[1], old[2]) != (incident, status, note.strip()):
                    raise ValueError("Cle de commentaire deja utilisee pour un autre contenu.")
            else:
                c.execute("UPDATE incidents SET statut=? WHERE id=?", (status, incident))
                self._journal(c, incident, status, note.strip(), key)
        return next(x for x in self.incidents() if x["id"] == incident)

    def save_experiment(self, kind, parameters, result):
        identity = uuid4().hex
        with self.connection() as c:
            c.execute("INSERT INTO experiences VALUES(?,?,?,?,?)", (identity, kind, dumps(parameters), dumps(result), utc()))
        return identity

    def experiments(self):
        with self.connection() as c:
            return [dict(r) for r in c.execute("SELECT id,type,parametres_json,cree_utc FROM experiences ORDER BY cree_utc DESC LIMIT 50")]

    def experiment(self, identity):
        with self.connection() as c:
            row = c.execute("SELECT * FROM experiences WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("Experience inconnue.")
            return {"id": row["id"], "type": row["type"], "parametres": json.loads(row["parametres_json"]),
                    "resultat": json.loads(row["resultat_json"]), "cree_utc": row["cree_utc"]}


def sensor_plan(budget=9, weights=None):
    """Enumeration exacte de 16 sous-ensembles, sans utiliser les tests.

    Cinq mesures historiques conservees + deux debits par conduite choisie.
    L'objectif est une couverture ponderee, pas une garantie de detection.
    """
    weights = weights or {p: 1. for p in PIPES}
    if isinstance(budget, bool) or not isinstance(budget, int) or not 5 <= budget <= 13:
        raise ValueError("Budget entre 5 et 13 mesures.")
    if set(weights) != set(PIPES) or any(not number(w) or not 0 < w <= 100 for w in weights.values()):
        raise ValueError("Poids strictement positifs jusqu'a 100 pour P2 a P5.")
    plans = []
    for n in range(5):
        for subset in combinations(PIPES, n):
            cost = 5 + 2 * n
            if cost <= budget:
                plans.append({"paires": list(subset), "mesures": cost,
                    "score_couverture": sum(weights[p] for p in subset), "couverture_conduites": n / 4})
    plans.sort(key=lambda a: (-a["score_couverture"], a["mesures"], a["paires"]))
    best = plans[0]
    return {"origine": "etude_theorique_sur_la_maquette_V3", "budget": budget, "poids_utilisateur": weights,
        "plan": best, "plans_comparables": plans,
        "mesures_a_installer": list(OPTIONAL) + [f"debit_{p}{s}_m3_s" for p in best["paires"] for s in ("", "_aval")],
        "limite": "Couverture des conduites par des paires completes ; aucun score de fuite ni delai garanti. Les cinq mesures de base ne sont pas utilisees pour remplacer une paire manquante. Poids choisis avant consultation des resultats historiques."}
