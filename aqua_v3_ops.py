"""Operations et moteur deterministe de securite du laboratoire SIMULE.

Les contraintes sont des choix d'exercice, pas une certification de surete.
On verifie les etats quasi-statiques prevus, pas les coups de belier. Aucune
sortie materielle n'existe. Un accord operateur n'autorise pas de contourner
la qualite, la fraicheur ou les limites de pression des zones alimentees.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

from aqua_v2_core import dumps, utc
from aqua_v2_hydraulique import solve, choose
from aqua_v3_monitor import digest

TRANSITIONS = {"planifiee": {"en_cours", "annulee"}, "en_cours": {"a_verifier", "annulee"},
               "a_verifier": {"en_cours", "cloturee"}, "cloturee": set(), "annulee": set()}
POLICY = {"pression_min_m": 10., "pression_max_m": 45., "variation_max_points_par_300s": 10.,
          "ouverture_min_pct": 5., "zones_critiques": ["B"], "fraicheur_max_s": 120., "expiration_proposition_s": 300.}


def default_context():
    return {"version": 0, "charge_source_m": 55., "demande_A_l_s": 6., "demande_B_l_s": 4.,
            "ouvertures_pct": {"A": 100., "B": 100.}, "zones_isolees": [],
            "hypothese_fuite_zone": "", "hypothese_fuite_mm": 0.,
            "qualite_capteurs": True, "communication": True, "mode": "assiste",
            "recu_epoch_s": time.time(), "phase": "pret", "derniere_action": None,
            "usagers_A": None, "usagers_B": None,
            "limite": "Contexte du laboratoire distinct du rejeu des capteurs V3 ; parametres d'experience declares."}


def simulation(context, openings=None, isolated=None):
    o = openings or context["ouvertures_pct"]
    return solve(context["charge_source_m"], context["demande_A_l_s"]/1000,
                 context["demande_B_l_s"]/1000, o["A"]/100, o["B"]/100,
                 leak_zone=context["hypothese_fuite_zone"], leak_mm=context["hypothese_fuite_mm"],
                 isolate=tuple(context["zones_isolees"] if isolated is None else isolated))


def evaluate(context, action, critical_authorized=False, justification="", now=None):
    """Evaluation pure. Aucune ecriture, aucun acces aux etiquettes de fuite."""
    now = time.time() if now is None else now
    if action not in {"equilibrer", "reduire_A", "reduire_B", "isoler_A", "isoler_B", "retablir"}:
        raise ValueError("Action inconnue.")
    reasons = []
    if not context["qualite_capteurs"]:
        reasons.append("Qualite des capteurs insuffisante : mouvement bloque.")
    if not context["communication"]:
        reasons.append("Communication perdue : maintien de la derniere consigne, etat non garanti sur materiel.")
    if now-context["recu_epoch_s"] > POLICY["fraicheur_max_s"] or now < context["recu_epoch_s"]-2:
        reasons.append("Observation perimee ou horloge incoherente : actualiser le laboratoire.")
    if context["phase"] != "pret":
        reasons.append("Verification de la precedente action requise.")
    if context["mode"] == "manuel":
        reasons.append("Mode manuel : les propositions ne sont pas executables.")
    before = simulation(context)
    settings = deepcopy(context["ouvertures_pct"])
    isolated = list(context["zones_isolees"])
    if action == "equilibrer":
        target, decision = choose(context["charge_source_m"], context["demande_A_l_s"]/1000,
                                 context["demande_B_l_s"]/1000, (settings["A"]/100, settings["B"]/100), before,
                                 context["qualite_capteurs"])
        settings = dict(zip("AB", (x*100 for x in target)))
        if decision.startswith("gel_"):
            reasons.append("Regulateur suspendu : "+decision)
    elif action.startswith("reduire_"):
        z = action[-1]
        settings[z] = max(5., settings[z]-20.)
    elif action.startswith("isoler_"):
        z = action[-1]
        if z not in isolated:
            isolated.append(z)
        if z in POLICY["zones_critiques"] and not (critical_authorized and len(justification.strip()) >= 10):
            reasons.append("Zone critique B : autorisation explicite et justification de 10 caracteres requises pour l'isolement simule.")
        if context["mode"] == "automatique":
            reasons.append("Isolement interdit en mode automatique ; passer en mode assiste.")
    elif action == "retablir":
        isolated = []
    delta = max(abs(settings[z]-context["ouvertures_pct"][z]) for z in "AB")
    n = max(1, int(__import__('math').ceil((delta-1e-8)/10)))
    steps = []
    for i in range(1, n+1):
        opening = {z: context["ouvertures_pct"][z]+(settings[z]-context["ouvertures_pct"][z])*i/n for z in "AB"}
        state = simulation(context, opening, isolated)
        checks = []
        for z, value in state["zones"].items():
            if z not in isolated and not POLICY["pression_min_m"] <= value["pression_m"] <= POLICY["pression_max_m"]:
                checks.append(f"Pression {z} prevue {value['pression_m']:.2f} mCE hors [{POLICY['pression_min_m']}, {POLICY['pression_max_m']}].")
        if any(not POLICY["ouverture_min_pct"] <= value <= 100 for value in opening.values()):
            checks.append("Ouverture de vanne modulante hors limites.")
        reasons.extend(checks)
        steps.append({"pas": i, "apres_s": 300*i, "ouvertures_pct": opening, "zones_isolees": isolated, "prediction": state})
    after = steps[-1]["prediction"]
    cut = [z for z, value in after["zones"].items() if value["fraction_servie"] < .05]
    populations = [context.get("usagers_"+z) for z in cut]
    return {"autorisee": not reasons, "motifs_refus": list(dict.fromkeys(reasons)), "action": action,
            "contexte_version": context["version"], "avant": before, "apres": after,
            "etapes": steps, "ouvertures_cibles_pct": settings, "zones_isolees_cibles": isolated,
            "vannes_isolement": [f"ISO_{z}_{side}" for z in isolated for side in ("amont", "aval")],
            "gain_fuite_simule_l_s": before["debit_fuite_simule_l_s"]-after["debit_fuite_simule_l_s"],
            "zones_privees_service": [z for z, values in after["zones"].items() if values["fraction_servie"] < .05],
            "usagers_zones_privees_service": sum(populations) if all(v is not None for v in populations) else None,
            "interpretation_usagers": "Effectifs declares des zones quasi entierement privees de service ; nombre inconnu en cas de desserte partielle ou d'effectif non renseigne.",
            "politique": POLICY, "origine": "laboratoire hydraulique simule",
            "limite": "Controle des etats prevus quasi-statiques seulement. Les deux vannes d'isolement sont binaires ideales ; leurs transitoires et temps de course ne sont pas modelises. La limite de vitesse porte sur les vannes modulantes. Aucun ordre materiel."}


class Operations:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS documents(cle TEXT PRIMARY KEY,contenu TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS interventions(id TEXT PRIMARY KEY,contenu TEXT NOT NULL,cle TEXT UNIQUE NOT NULL,empreinte TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS propositions(id TEXT PRIMARY KEY,contenu TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(numero INTEGER PRIMARY KEY AUTOINCREMENT,cree_utc TEXT NOT NULL,type TEXT NOT NULL,contenu TEXT NOT NULL,precedente TEXT NOT NULL,empreinte TEXT NOT NULL);
            """)
            c.execute("INSERT OR IGNORE INTO documents VALUES('contexte',?)", (dumps(default_context()),))

    @contextmanager
    def db(self):
        with self.lock:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("BEGIN IMMEDIATE")
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def _get(self, c, key, default=None):
        row = c.execute("SELECT contenu FROM documents WHERE cle=?", (key,)).fetchone()
        return json.loads(row[0]) if row else deepcopy(default)

    def _set(self, c, key, value):
        c.execute("INSERT INTO documents VALUES(?,?) ON CONFLICT(cle) DO UPDATE SET contenu=excluded.contenu", (key, dumps(value)))

    def _audit(self, c, kind, data):
        previous = c.execute("SELECT empreinte FROM audit ORDER BY numero DESC LIMIT 1").fetchone()
        previous = previous[0] if previous else "0"*64
        timestamp, payload = utc(), dumps(data)
        fingerprint = digest([timestamp, kind, payload, previous])
        c.execute("INSERT INTO audit(cree_utc,type,contenu,precedente,empreinte) VALUES(?,?,?,?,?)", (timestamp,kind,payload,previous,fingerprint))

    def get(self, key, default=None):
        with self.db() as c:
            return self._get(c, key, default)

    def put(self, key, value):
        with self.db() as c:
            self._set(c, key, value)
            self._audit(c, "document", {"cle": key, "sha256": digest(value)})
        return value

    def complement(self, sid, seq, values):
        with self.db() as c:
            key = "complements:"+sid
            data = self._get(c, key, {})
            old = data.get(str(seq))
            if old is not None and old != values:
                raise ValueError("Complement deja enregistre pour cette sequence ; le remplacer altererait les preuves.")
            if old is None:
                data[str(seq)] = values
                self._set(c, key, data)
                self._audit(c,"complement", {"session": sid,"sequence":seq,"valeurs":values})
        return values

    def context(self):
        context = self.get("contexte")
        return {**context, "observation_simulee": simulation(context), "politique": POLICY,
                "communication_perdue_action": "Maintenir la consigne et refuser les nouveaux mouvements ; aucune position de securite physique garantie."}

    def set_context(self, values, version):
        with self.db() as c:
            ctx = self._get(c, "contexte")
            if ctx["version"] != version:
                raise ValueError("Contexte modifie ; relire avant de sauvegarder.")
            if ctx["phase"] == "attente_verification":
                raise ValueError("Verifier la derniere action avant de modifier le contexte.")
            ctx.update(values)
            ctx.update(version=version+1, recu_epoch_s=time.time(), phase="pret")
            simulation(ctx)
            self._set(c, "contexte", ctx)
            self._audit(c, "contexte_actualise", ctx)
        return self.context()

    def propose(self, action, critical_authorized=False, justification=""):
        with self.db() as c:
            ctx = self._get(c, "contexte")
            review = evaluate(ctx, action, critical_authorized, justification)
            p = {"id": uuid4().hex, "cree_epoch_s": time.time(), "etat": "proposee" if review["autorisee"] else "refusee",
                 "action": action, "autorisation_critique": critical_authorized, "justification": justification,
                 "contexte_sha256": digest(ctx), "evaluation": review}
            c.execute("INSERT INTO propositions VALUES(?,?)", (p["id"], dumps(p)))
            self._audit(c, "proposition", p)
        return p

    def proposals(self):
        with self.db() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT contenu FROM propositions ORDER BY rowid DESC LIMIT 100")]

    def execute(self, identity, approve):
        if approve is not True:
            raise ValueError("Validation explicite de l'operateur requise dans le laboratoire.")
        with self.db() as c:
            row = c.execute("SELECT contenu FROM propositions WHERE id=?", (identity,)).fetchone()
            if not row:
                raise KeyError("Proposition inconnue.")
            p = json.loads(row[0])
            # Rejouer la meme action ne provoque jamais un second mouvement.
            if p["etat"] in ("executee_simulation", "verifiee_simulation", "ecart_observe"):
                return dict(p, requete_rejouee=True)
            if p["etat"] != "proposee":
                raise ValueError("Proposition refusee ou non executable.")
            ctx = self._get(c, "contexte")
            if time.time()-p["cree_epoch_s"] > POLICY["expiration_proposition_s"] or digest(ctx) != p["contexte_sha256"]:
                raise ValueError("Proposition perimee ou contexte modifie ; recalculer.")
            review = evaluate(ctx, p["action"], p["autorisation_critique"], p["justification"])
            if not review["autorisee"]:
                raise ValueError("Action bloquee : "+" ".join(review["motifs_refus"]))
            p.update(etat="executee_simulation", execute_epoch_s=time.time(), evaluation=review,
                     contexte_avant=deepcopy(ctx), requete_rejouee=False)
            ctx.update(ouvertures_pct=review["ouvertures_cibles_pct"], zones_isolees=review["zones_isolees_cibles"],
                       phase="attente_verification", derniere_action=identity, version=ctx["version"]+1)
            self._set(c, "contexte", ctx)
            c.execute("UPDATE propositions SET contenu=? WHERE id=?", (dumps(p),identity))
            self._audit(c,"execution_simulee",p)
        return p

    def verify(self, identity, fault="aucun"):
        """Nouveau retour genere par le laboratoire, jamais appele mesure terrain."""
        if fault not in {"aucun", "vanne_bloquee", "capteur_absent", "reponse_lente"}:
            raise ValueError("Defaut de verification inconnu.")
        with self.db() as c:
            row = c.execute("SELECT contenu FROM propositions WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError("Proposition inconnue.")
            p = json.loads(row[0])
            if p["etat"] in ("verifiee_simulation", "ecart_observe"):
                if p["verification"]["defaut"] != fault:
                    raise ValueError("Verification deja figee avec un autre retour.")
                return p
            if p["etat"] != "executee_simulation":
                raise ValueError("Executer d'abord l'action simulee.")
            ctx = self._get(c, "contexte")
            observed = simulation(p["contexte_avant"] if fault == "vanne_bloquee" else ctx) if fault != "capteur_absent" else None
            expected = p["evaluation"]["apres"]
            planned_duration = len(p["evaluation"]["etapes"])*300
            elapsed = planned_duration+(600 if fault == "reponse_lente" else 0)
            differences = {z: abs(observed["zones"][z]["pression_m"]-expected["zones"][z]["pression_m"]) for z in "AB"} if observed else {}
            good = fault == "aucun" and observed is not None and all(v <= .5 for v in differences.values())
            p["verification"] = {"defaut": fault, "recu_epoch_s": max(time.time(),p["execute_epoch_s"]+.000001),
                 "mesure_simulee": observed, "ecarts_pression_m": differences, "conforme": good,
                 "duree_manoeuvre_prevue_s":planned_duration, "delai_retour_simule_s":elapsed,
                 "sante_vanne": "reponse_conforme_dans_le_simulateur" if good else ("retour_position_bloquee" if fault == "vanne_bloquee" else ("retour_trop_lent" if fault == "reponse_lente" else "retour_indisponible")),
                 "limite": "Retour genere par le meme modele hydraulique, sans validation independante de l'actionneur ni capteur reel."}
            p["etat"] = "verifiee_simulation" if good else "ecart_observe"
            if good:
                ctx.update(phase="pret", recu_epoch_s=time.time(), version=ctx["version"]+1)
            else:
                ctx.update(phase="gel_ecart_observe", qualite_capteurs=False, version=ctx["version"]+1)
                if fault == "vanne_bloquee":
                    ctx["ouvertures_pct"] = p["contexte_avant"]["ouvertures_pct"]
                    ctx["zones_isolees"] = p["contexte_avant"]["zones_isolees"]
            self._set(c,"contexte",ctx)
            c.execute("UPDATE propositions SET contenu=? WHERE id=?",(dumps(p),identity))
            self._audit(c,"verification_post_action",p)
        return p

    def create_order(self, data, snapshot, key):
        fingerprint = digest(data)
        with self.db() as c:
            old = c.execute("SELECT contenu,empreinte FROM interventions WHERE cle=?",(key,)).fetchone()
            if old:
                if old[1] != fingerprint:
                    raise ValueError("Cle d'intervention deja utilisee pour un autre contenu.")
                return json.loads(old[0])
            item = {"id":uuid4().hex, **data, "statut":"planifiee", "version":0, "cree_utc":utc(),
                    "sequence_creation":snapshot["sequence"], "preuve_avant":snapshot,
                    "journal":[{"statut":"planifiee","note":data["description"],"date":utc()}]}
            c.execute("INSERT INTO interventions VALUES(?,?,?,?)",(item["id"],dumps(item),key,fingerprint))
            self._audit(c,"intervention_creee",{"id":item["id"],"contenu":data})
        return item

    def orders(self):
        with self.db() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT contenu FROM interventions ORDER BY rowid DESC LIMIT 300")]

    def order(self, identity):
        with self.db() as c:
            r = c.execute("SELECT contenu FROM interventions WHERE id=?",(identity,)).fetchone()
            if r is None:
                raise KeyError("Intervention inconnue.")
            return json.loads(r[0])

    def update_order(self, identity, status, note, version, snapshot=None, clean_confirmed=False):
        with self.db() as c:
            r = c.execute("SELECT contenu FROM interventions WHERE id=?",(identity,)).fetchone()
            if r is None:
                raise KeyError("Intervention inconnue.")
            item = json.loads(r[0])
            if item["version"] != version:
                raise ValueError("Intervention modifiee ; relire avant de changer son statut.")
            if status not in TRANSITIONS.get(item["statut"], set()):
                raise ValueError("Transition de statut interdite.")
            if status == "cloturee":
                if not snapshot or snapshot["sequence"] <= item.get("sequence_a_verifier", item["sequence_creation"]) or not clean_confirmed:
                    raise ValueError("Cloture refusee : recevoir au moins trois nouvelles mesures utilisables sans alerte apres demande de verification.")
                item["preuve_apres"] = snapshot
                item["limite_cloture"] = "Retour de trois mesures au regime sans alerte, dans le rejeu simule ; ne prouve pas une reparation physique."
            if status == "a_verifier":
                item["sequence_a_verifier"] = snapshot["sequence"] if snapshot else item["sequence_creation"]
            item.update(statut=status,version=version+1)
            item["journal"].append({"statut":status,"note":note,"date":utc()})
            c.execute("UPDATE interventions SET contenu=? WHERE id=?",(dumps(item),identity))
            self._audit(c,"intervention_statut",{"id":identity,"statut":status,"version":version+1,"note":note})
        return item

    def audit(self):
        with self.db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM audit ORDER BY numero")]
        previous, good = "0"*64, True
        for r in rows:
            good &= r["precedente"] == previous and r["empreinte"] == digest([r["cree_utc"],r["type"],r["contenu"],previous])
            previous = r["empreinte"]
        return {"integrite_chaine": bool(good), "evenements": rows,
                "limite": "Chaine locale de hachages ; aucun ancrage externe, pas de protection contre une reecriture complete par un administrateur."}
