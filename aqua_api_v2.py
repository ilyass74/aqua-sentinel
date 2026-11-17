r"""AQUA Sentinel V2 : six extensions et regulation de vannes EN SIMULATION.

  .venv\Scripts\python.exe aqua_api_v2.py verifier
  .venv\Scripts\python.exe aqua_api_v2.py servir --ouvrir

Conserve les routes, les codes et les modeles V1/V3. Ajoute un journal
SQLite, une ingestion causale persistante, le controle des mesures, les
bilans de volumes, le placement par paires et le laboratoire a deux zones.
Source du flux de demonstration : mesures V3 deja sauvegardees, transmises
progressivement au detecteur V2 ; ce n'est pas une collecte de terrain.
Les nouvelles experiences hydrauliques sont distinctes du test V3 historique.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import csv
import json
from pathlib import Path
import socket
import threading
import time
from typing import Any, Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

import aqua_api_v1 as v1
from aqua_v2_core import Store, PIPES, FLOWS, sensor_plan, utc
from aqua_v2_hydraulique import SCENARIOS, regulation, interventions

ROOT = Path(__file__).resolve().parent
PORT = 8013


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class SessionInput(Contract):
    origine: Literal["simulation"]
    scenario_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    source: Literal["rejeu", "api"] = "rejeu"
    debut_index: StrictInt = Field(default=0, ge=0, le=1439)
    perturbation: Literal["aucune", "manquant", "fige", "biais", "desynchronise"] = "aucune"
    capteur_cible: Literal["debit_P2_m3_s", "debit_P2_aval_m3_s", "debit_P3_m3_s", "debit_P3_aval_m3_s", "debit_P4_m3_s", "debit_P4_aval_m3_s", "debit_P5_m3_s", "debit_P5_aval_m3_s"] = "debit_P3_aval_m3_s"


class AdvanceInput(Contract):
    nombre: StrictInt = Field(default=1, ge=1, le=120)


class PlaybackInput(Contract):
    active: bool


class MeasureInput(Contract):
    origine: Literal["simulation"]
    sequence: StrictInt = Field(ge=0)
    mesure: dict[str, Any]
    corrections_l_s: dict[str, float] = Field(default_factory=dict)
    horodatages: dict[str, StrictInt] = Field(default_factory=dict)


class NoteInput(Contract):
    statut: Literal["nouveau", "a_verifier", "inspection_planifiee", "en_intervention", "resolu"]
    note: str = Field(min_length=1, max_length=1000)
    cle: str = Field(min_length=8, max_length=100)


class RegulationInput(Contract):
    origine: Literal["simulation"]
    scenario: Literal["deux_zones", "pointe_B", "ressource_limitee", "capteur_absent", "fuite_A"] = "pointe_B"


class InterventionInput(Contract):
    origine: Literal["simulation"]
    zone: Literal["A", "B"] = "A"
    ouverture_mm: float = Field(default=8., ge=4, le=12)
    duree_min: StrictInt = Field(default=30, ge=5, le=120)


class PlanInput(Contract):
    budget: StrictInt = Field(default=9, ge=5, le=13)
    poids: dict[str, float] = Field(default_factory=lambda: {p: 1. for p in PIPES})


class ServiceV2:
    def __init__(self, base, db_path):
        self.base = base
        self.store = Store(db_path, base.modele_detection["calibration"])
        self.feed_lock = threading.RLock()
        self.study_lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.deadlines = {}

    def start(self):
        self.thread = threading.Thread(target=self._worker, name="aqua-simulation-source", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def create(self, body):
        if body.source == "rejeu" and body.scenario_id not in self.base.groupes_capteurs:
            raise KeyError("Journee de rejeu inconnue.")
        if body.source == "api" and (body.debut_index or body.perturbation != "aucune"):
            raise ValueError("La source API ne transforme pas les mesures envoyees par le producteur.")
        return self.store.create(body.scenario_id, body.debut_index,
            {"source": body.source, "perturbation": body.perturbation, "capteur_cible": body.capteur_cible,
             "defaut_debut_sequence": 10, "defaut_fin_sequence": 20,
             "cadence_reelle_s": .5, "pas_simule_s": 60})

    def advance(self, sid, count=1):
        with self.feed_lock:
            session = self.store.session(sid)
            if session["configuration"]["source"] != "rejeu":
                raise ValueError("Cette session recoit les mesures par POST ; aucun rejeu automatique.")
            frame = self.base.groupes_capteurs[session["scenario_id"]]
            results = []
            for _ in range(count):
                session = self.store.session(sid)
                seq = session["sequence"]
                index = session["debut_index"] + seq
                if index >= len(frame):
                    self.store.autoplay(sid, False)
                    break
                row = v1.enregistrements(frame.iloc[index:index+1])[0]
                config = session["configuration"]
                clocks = {}
                if config["defaut_debut_sequence"] <= seq < config["defaut_fin_sequence"]:
                    name = config["capteur_cible"]
                    mode = config["perturbation"]
                    if mode == "manquant":
                        row[name] = None
                    elif mode == "fige":
                        # La valeur ancree est a un instant deja atteint par la source.
                        row[name] = float(frame.iloc[session["debut_index"] + config["defaut_debut_sequence"]][name])
                    elif mode == "biais":
                        row[name] += .001
                    elif mode == "desynchronise":
                        clocks[name] = row["temps_s"] - 60
                results.append(self.store.ingest(sid, seq, row, horodatages=clocks))
            current = self.store.session(sid)
            return {"session": current, "mesures_ajoutees": len(results),
                    "terminee": current["debut_index"] + current["sequence"] >= len(frame)}

    def playback(self, sid, active):
        with self.feed_lock:
            s = self.store.session(sid)
            if s["configuration"]["source"] != "rejeu":
                raise ValueError("La lecture automatique concerne uniquement la source de rejeu.")
            return self.store.autoplay(sid, active)

    def _worker(self):
        while not self.stop.wait(.2):
            now = time.monotonic()
            for session in self.store.sessions():
                sid = session["id"]
                if session["automatique"] and now >= self.deadlines.get(sid, 0):
                    try:
                        with self.feed_lock:
                            if self.store.session(sid)["automatique"]:
                                self.advance(sid)
                    except Exception as exc:
                        self.store.autoplay(sid, False)
                        with self.store.connection() as c:
                            config = session["configuration"]
                            config["erreur_flux"] = str(exc)
                            c.execute("UPDATE sessions SET config_json=? WHERE id=?", (json.dumps(config), sid))
                    self.deadlines[sid] = now + .5

    def plan(self, body):
        result = sensor_plan(body.budget, body.poids)
        # Choix du plan termine AVANT lecture du rapport historique.
        path = self.base.detection_run / "localisation_test.csv"
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as handle:
                events = list(csv.DictReader(handle))
            kept = [r for r in events if r["tuyau_cible"] in result["plan"]["paires"] and r["detectee"].lower() == "true"]
            delays = sorted(float(r["delai_s"]) for r in kept)
            middle = len(delays)//2
            median = (delays[middle] if len(delays)%2 else .5*(delays[middle-1]+delays[middle])) if delays else None
            result["archives"] = {"fuites_historiques": len(events), "fuites_sur_conduites_couvertes": len(kept),
                "delai_median_archive_s": median,
                "sha256_rapport": v1.sha256(path), "test_independant": False,
                "interpretation": "Sous-selection descriptive du rapport V3 deja connu ; delais inchanges pour les paires conservees. Pas une nouvelle evaluation du placement ni une garantie avec de nouveaux capteurs."}
        else:
            result["archives"] = None
        return result


def creer_app(etat=None, racine=ROOT, db_path=None, worker=True):
    racine = Path(racine)
    api = v1.creer_app(etat, racine)
    original_lifespan = api.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            application.state.v2 = ServiceV2(application.state.aqua,
                db_path or racine / "resultats_aqua/extensions_v2/aqua_v2.sqlite3")
            if worker:
                application.state.v2.start()
            try:
                yield
            finally:
                application.state.v2.close()

    api.router.lifespan_context = lifespan
    api.title = "AQUA Sentinel V2"
    api.version = "2.0"
    api.router.routes = [r for r in api.router.routes if getattr(r, "path", None) != "/"]

    def invoke(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @api.get("/", include_in_schema=False)
    def page():
        return FileResponse(racine / "aqua_interface_v2.html", media_type="text/html; charset=utf-8")

    @api.get("/v1", include_in_schema=False)
    def page_v1():
        return FileResponse(racine / "aqua_interface_v1.html", media_type="text/html; charset=utf-8")

    @api.get("/assets/aqua_v2.js", include_in_schema=False)
    def javascript():
        return FileResponse(racine / "aqua_interface_v2.js", media_type="text/javascript; charset=utf-8")

    @api.get("/api/v2/sante")
    def health():
        return {"statut": "pret", "version": "2.0", "origine": "simulation", "commande_materielle": False,
            "modules": ["interventions", "qualite_capteurs", "volumes", "journal", "flux_persistant", "placement", "regulation_vannes"],
            "regulation_scenarios": SCENARIOS,
            "limite_biais": "Un biais de debitmetre peut etre indiscernable d'une fuite par les seuls bilans."}

    @api.get("/api/v2/sessions")
    def sessions():
        return {"sessions": api.state.v2.store.sessions()}

    @api.post("/api/v2/sessions", status_code=201)
    def create_session(body: SessionInput):
        return invoke(api.state.v2.create, body)

    @api.get("/api/v2/sessions/{sid}")
    def session(sid: str):
        return invoke(api.state.v2.store.session, sid)

    @api.post("/api/v2/sessions/{sid}/avancer")
    def advance(sid: str, body: AdvanceInput):
        return invoke(api.state.v2.advance, sid, body.nombre)

    @api.put("/api/v2/sessions/{sid}/lecture")
    def playback(sid: str, body: PlaybackInput):
        return invoke(api.state.v2.playback, sid, body.active)

    @api.post("/api/v2/sessions/{sid}/mesures")
    def ingest(sid: str, body: MeasureInput):
        s = invoke(api.state.v2.store.session, sid)
        if s["configuration"]["source"] != "api":
            raise HTTPException(422, "Utiliser une session de source api pour l'ingestion externe.")
        return invoke(api.state.v2.store.ingest, sid, body.sequence, body.mesure, body.corrections_l_s, body.horodatages)

    @api.get("/api/v2/sessions/{sid}/courbe")
    def history(sid: str):
        return {"mesures": invoke(api.state.v2.store.history, sid)}

    @api.get("/api/v2/incidents")
    def incidents():
        return {"origine": "simulation", "incidents": api.state.v2.store.incidents()}

    @api.post("/api/v2/incidents/{identity}/notes")
    def note(identity: str, body: NoteInput):
        return invoke(api.state.v2.store.annotate, identity, body.statut, body.note, body.cle)

    def study(kind, params, function, *args):
        s = api.state.v2
        if not s.study_lock.acquire(blocking=False):
            raise HTTPException(409, "Une experience hydraulique est deja en cours ; patienter avant de relancer.")
        try:
            result = invoke(function, *args)
            identity = s.store.save_experiment(kind, params, result)
            return {"experience_id": identity, **result}
        finally:
            s.study_lock.release()

    @api.post("/api/v2/regulation")
    def regulate(body: RegulationInput):
        return study("regulation", body.model_dump(), regulation, body.scenario)

    @api.post("/api/v2/interventions")
    def intervene(body: InterventionInput):
        return study("intervention", body.model_dump(), interventions, body.zone, body.ouverture_mm, body.duree_min)

    @api.post("/api/v2/capteurs/plan")
    def placement(body: PlanInput):
        return invoke(api.state.v2.plan, body)

    @api.get("/api/v2/experiences")
    def experiments():
        return {"experiences": api.state.v2.store.experiments()}

    @api.get("/api/v2/experiences/{identity}")
    def experiment(identity: str):
        return invoke(api.state.v2.store.experiment, identity)

    @api.get("/api/v2/export")
    def export():
        s = api.state.v2
        return {"origine": "simulation", "export_utc": utc(), "sessions": s.store.sessions(),
            "incidents": s.store.incidents(), "experiences": s.store.experiments(),
            "resultats_historiques_V1": api.state.aqua.resume()}

    return api


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["verifier", "servir"])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--ouvrir", action="store_true")
    parser.add_argument("--base", type=Path, help="Fichier SQLite V2, conserve entre les lancements.")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port entre 1024 et 65535 requis.")
    etat = v1.EtatAqua(ROOT)
    if args.action == "verifier":
        from aqua_verifier_v2 import verifier
        verifier(etat)
        return
    for name in ["aqua_interface_v2.html", "aqua_interface_v2.js"]:
        if not (ROOT / name).is_file():
            parser.error("Fichier V2 absent : " + name)
    with socket.socket() as probe:
        try:
            probe.bind((v1.HOTE, args.port))
        except OSError:
            parser.error(f"Port {args.port} deja utilise ; essayer --port {args.port+1}.")
    if args.ouvrir:
        threading.Thread(target=v1.ouvrir_quand_pret, args=(args.port,), daemon=True).start()
    import uvicorn
    print(f"AQUA Sentinel V2 : http://{v1.HOTE}:{args.port}")
    print("Simulation uniquement ; conserver cette fenetre ouverte. Ctrl+C pour arreter.")
    uvicorn.run(creer_app(etat, ROOT, args.base), host=v1.HOTE, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
