r"""AQUA Sentinel — API locale et interface de consultation.

Lire avant integration. Placer a cote des scripts AQUA existants et de
aqua_interface_v1.html, puis installer requirements_api_v1.txt.
  .venv\Scripts\python.exe aqua_api_v1.py verifier
  .venv\Scripts\python.exe aqua_api_v1.py servir --ouvrir
  Adresse par defaut : http://127.0.0.1:8012

Cette API utilise les modeles FIGES de detection V3 et de risque V1.
Elle ne reentraine, ne regle et ne regenere aucun test. Les fichiers de
verite des tests ne sont jamais lus par ce service. Les scores de bilan
et de risque proviennent des rapports deja evalues, comme resultats
historiques de simulations. Le reseau affiche est un schema de la maquette.

Rejeu : les 64 journees de capteurs et les predictions V3 sauvegardees.
Le curseur choisit un instant de cette journee, pas l'heure actuelle.
Les empreintes des capteurs et predictions sont controlees au demarrage.
Risque : recalcul sur les 9 observations FICTIVES de l'illustration,
avec le modele risque fige ; aucun risque reel de rupture n'est estime.

POST /api/v1/detection/predire : inference sans etat persistant, sur un
historique continu au pas de 60 s fourni dans CHAQUE requete. Le lissage
et la persistance repartent au debut du lot ; les 2 premiers points sont
neutres. Envoyer l'historique complet, pas une mesure isolee en esperant
conserver l'etat d'une requete precedente. Origine 'simulation' requise.

POST /api/v1/risque/predire : observations de l'origine simulation_actifs
uniquement. Schema/variables/verifications du module risque V1 conserves.
Le quota propose 20 % des actifs de chaque date, arrondi au superieur.
Il ne constitue pas un seuil de danger ; l'indice 0..100 est relatif.

Lancement local seulement (127.0.0.1). Aucun ordre de vanne ou de pompe,
aucune collecte terrain, aucune notification externe. Pas d'authentification
ni de deploiement public dans cette version. Le serveur est bloque si un
code/modele fige manque ou differe de son empreinte. Ctrl+C pour arreter.

Sources de l'implementation :
https://fastapi.tiangolo.com/advanced/events/
https://fastapi.tiangolo.com/tutorial/body/
https://fastapi.tiangolo.com/tutorial/testing/
https://fastapi.tiangolo.com/advanced/custom-response/
"""

import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import threading
import time
from typing import Any, Literal
from urllib.request import urlopen
from uuid import uuid4
import webbrowser

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field


DOSSIER = Path(__file__).resolve().parent
HOTE = "127.0.0.1"
PORT = 8012
TUYAUX = ["P2", "P3", "P4", "P5"]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lire_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def enregistrements(frame):
    return json.loads(frame.to_json(orient="records", force_ascii=False, double_precision=12))


def dernier(racine):
    candidats = sorted([p for p in Path(racine).glob("*") if (p / "_TEST_EVALUE").is_file()], reverse=True)
    if not candidats:
        raise ValueError("Aucun modele evalue dans " + str(racine))
    return candidats[0]


def episodes(temps, alerte, cibles):
    episodes_liste = []
    debut = None
    for i in range(len(temps) + 1):
        actif = i < len(temps) and bool(alerte[i])
        if actif and debut is None:
            debut = i
        if not actif and debut is not None:
            episodes_liste.append({"debut_s": int(temps[debut]),
                "fin_s_exclusive": int(temps[i]) if i < len(temps) else int(temps[-1] + 60),
                "tuyau_premier_signal": str(cibles[debut])})
            debut = None
    return episodes_liste


def reseau():
    return {"nature": "schema de la maquette ; positions graphiques, pas coordonnees geographiques",
        "noeuds": [{"id": k, "x": x, "y": y, "type": typ} for k, x, y, typ in
            [("R", 75, 180, "reservoir"), ("A", 235, 180, "jonction"),
             ("B", 440, 75, "jonction"), ("C", 440, 285, "jonction"), ("D", 660, 180, "jonction")]],
        "conduites": [{"id": p, "amont": u, "aval": v, "longueur_m": l, "diametre_mm": d,
                       "surveillee_par_paire": p in TUYAUX} for p, u, v, l, d in
            [("P1", "R", "A", 200, 150), ("P2", "A", "B", 120, 100),
             ("P3", "A", "C", 120, 100), ("P4", "B", "D", 80, 100), ("P5", "C", "D", 80, 100)]],
        "limite_localisation": "Conduite candidate entre deux points de mesure, pas position exacte du trou."}


class EtatAqua:
    def __init__(self, racine=DOSSIER, detection=None, risque=None):
        # Les imports utilisent les memes sources que les scripts figes.
        import aqua_detection_v3 as detecteur
        import aqua_risque_v1 as risque_module
        self.racine = Path(racine).resolve()
        self.detecteur, self.risque_module = detecteur, risque_module
        self.detection_run = Path(detection).resolve() if detection else dernier(self.racine / "models/aqua_detection_v3")
        self.risque_run = Path(risque).resolve() if risque else dernier(self.racine / "models/aqua_risque_v1")
        for run in [self.detection_run, self.risque_run]:
            if not (run / "_TEST_EVALUE").is_file() or not (run / "_GELE").is_file():
                raise ValueError("Modele non termine ou non evalue : " + str(run))
        self.meta_detection = lire_json(self.detection_run / "gel.json")
        detecteur.verifier_gel(self.detection_run, self.meta_detection)
        self.modele_detection = lire_json(self.detection_run / "modele.json")
        detecteur.verifier_modele(self.modele_detection)
        self.bilan_detection = lire_json(self.detection_run / "bilan_test.json")
        self.paquet_risque, self.meta_risque = risque_module.charger_modele(self.risque_run)
        if sha256(self.risque_run / "bilan_test.json") != (self.risque_run / "_TEST_EVALUE").read_text(encoding="utf-8").strip():
            raise ValueError("Bilan de risque modifie depuis son evaluation.")
        self.bilan_risque = lire_json(self.risque_run / "bilan_test.json")
        data = Path(self.bilan_detection["dataset_test"])
        if not (data / "_SUCCESS").is_file():
            raise ValueError("Dataset V3 incomplet ou absent : " + str(data))
        self.capteurs = detecteur.verifier_capteurs(data, self.bilan_detection["audit_dataset"])
        chemin_predictions = self.detection_run / "predictions_bilans13_test.csv"
        if sha256(chemin_predictions) != self.bilan_detection["sha256_predictions"]["bilans13"]:
            raise ValueError("Predictions V3 modifiees depuis leur evaluation.")
        self.predictions = pd.read_csv(chemin_predictions, keep_default_na=False).sort_values(detecteur.v1.CLES).reset_index(drop=True)
        if not self.predictions[detecteur.v1.CLES].equals(self.capteurs[detecteur.v1.CLES]):
            raise ValueError("Predictions et capteurs V3 desalignees.")
        if not self.predictions.alerte.isin([0, 1]).all():
            raise ValueError("Predictions V3 non binaires.")
        illustration = self.risque_run / "illustration_maquette/observations_fictives.csv"
        if not illustration.is_file():
            raise ValueError("Lancer d'abord : aqua_risque_v1.py illustrer")
        self.observations_risque = risque_module.verifier_entrees(pd.read_csv(illustration))
        attendus = {"P1", "P2", "P3", "P4", "P5", "J_A", "J_B", "J_C", "J_D"}
        if set(self.observations_risque.actif_id) != attendus or len(self.observations_risque) != 9:
            raise ValueError("L'illustration attend les cinq conduites et les quatre raccords fictifs.")
        self.priorites = self.predire_risque(self.observations_risque)
        self.liste_scenarios = []
        self.groupes_capteurs = {str(k): g.reset_index(drop=True) for k, g in self.capteurs.groupby("scenario_id", sort=True)}
        self.groupes_predictions = {str(k): g.reset_index(drop=True) for k, g in self.predictions.groupby("scenario_id", sort=True)}
        for i, (sid, p) in enumerate(self.groupes_predictions.items(), 1):
            actifs = p.loc[p.alerte.eq(1)]
            self.liste_scenarios.append({"scenario_id": sid, "libelle": f"Simulation {i:02d}",
                "contient_alerte": not actifs.empty,
                "premiere_alerte_s": int(actifs.temps_s.iloc[0]) if not actifs.empty else None,
                "debut_s": int(p.temps_s.iloc[0]), "fin_s": int(p.temps_s.iloc[-1]), "pas_s": 60})
        self.defaut = next((s for s in self.liste_scenarios if s["contient_alerte"]), self.liste_scenarios[0])
        self.empreintes = {str(f): sha256(f) for f in
            [self.detection_run / "modele.json", self.detection_run / "gel.json",
             self.detection_run / "bilan_test.json", chemin_predictions,
             self.risque_run / "modele.joblib", self.risque_run / "gel.json",
             self.risque_run / "bilan_test.json", illustration]}

    def predire_risque(self, x):
        r = self.risque_module
        p = r.predire(x, self.paquet_risque)
        details = x[r.CLES + ["age_ans", "classe_materiau", "etat_inspection", "reparations_passees"]].copy()
        details["facteurs_modele"] = r.expliquer(x, self.paquet_risque)
        return p.merge(details, on=r.CLES, validate="one_to_one").sort_values(["date_observation", "rang_dans_lot"]).reset_index(drop=True)

    def resume(self):
        return {"origine": "simulation", "validation_terrain": False,
            "detection": {"nature": "test hydraulique simule deja evalue", "modele": self.detection_run.name,
                "scores": self.bilan_detection["detection"]["Bilans_13"],
                "localisation": self.bilan_detection["localisation"],
                "limite": self.bilan_detection["limite"]},
            "risque": {"nature": "actifs et incidents artificiels, classement experimental", "modele": self.risque_run.name,
                "horizon_jours": self.bilan_risque["horizon_jours"],
                "quota_inspection": self.bilan_risque["quota_inspection"],
                "scores": self.bilan_risque["resultats"][self.meta_risque["retenu"]],
                "limite": self.bilan_risque["limite"]},
            "scenario_defaut": self.defaut, "reseau": reseau()}

    def scenario(self, sid):
        if sid not in self.groupes_capteurs:
            raise KeyError(sid)
        x, p = self.groupes_capteurs[sid], self.groupes_predictions[sid]
        return {"scenario_id": sid, "origine": "simulation", "mode": "rejeu_enregistre_pas_collecte_temps_reel",
            "temps_s": p.temps_s.astype(int).tolist(), "alerte": p.alerte.astype(int).tolist(),
            "tuyau_signale": p.tuyau_signale.tolist(),
            "pressions_m": {n: x[f"pression_{n}_m"].tolist() for n in "ABCD"},
            "debits_l_s": {t: (1000 * x[f"debit_{t}_m3_s"]).tolist() for t in ["P1"] + TUYAUX},
            "ecarts_l_s": {t: (1000 * (x[f"debit_{t}_m3_s"] - x[f"debit_{t}_aval_m3_s"])).tolist() for t in TUYAUX},
            "alertes_par_tuyau": {t: p[f"alerte_{t}"].astype(int).tolist() for t in TUYAUX},
            "episodes": episodes(p.temps_s.to_numpy(), p.alerte.to_numpy(), p.tuyau_signale.to_numpy()),
            "precision_localisation": "Conduite candidate entre les deux points de mesure."}


class DetectionEntree(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origine: Literal["simulation"]
    mesures: list[dict[str, Any]] = Field(min_length=1, max_length=1440,
        description="Historique continu au pas de 60 s ; etat du detecteur recalcule a chaque requete.")


class RisqueEntree(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origine: Literal["simulation_actifs"]
    observations: list[dict[str, Any]] = Field(min_length=1, max_length=2000)


def creer_app(etat=None, racine=DOSSIER, detection=None, risque=None):
    @asynccontextmanager
    async def lifespan(application):
        application.state.aqua = etat if etat is not None else EtatAqua(racine, detection, risque)
        yield

    api = FastAPI(title="AQUA Sentinel", version="1.0", lifespan=lifespan,
        description="API locale experimentale. Rejeu hydraulique et risque sur actifs fictifs. Aucune validation terrain.")

    @api.middleware("http")
    async def entetes(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.get("/", include_in_schema=False)
    def interface():
        return FileResponse(Path(racine) / "aqua_interface_v1.html", media_type="text/html; charset=utf-8")

    @api.get("/api/v1/sante")
    def sante():
        s = api.state.aqua
        return {"statut": "pret", "origine": "simulation", "validation_terrain": False,
            "scenarios_charges": len(s.liste_scenarios), "actifs_fictifs": len(s.priorites),
            "modele_detection": s.detection_run.name, "modele_risque": s.risque_run.name}

    @api.get("/api/v1/resume")
    def resume():
        return api.state.aqua.resume()

    @api.get("/api/v1/scenarios")
    def scenarios():
        return {"origine": "simulation", "scenarios": api.state.aqua.liste_scenarios}

    @api.get("/api/v1/scenarios/{scenario_id}")
    def scenario(scenario_id: str):
        try:
            return api.state.aqua.scenario(scenario_id)
        except KeyError:
            raise HTTPException(404, "Simulation inconnue.") from None

    @api.get("/api/v1/priorites")
    def priorites():
        s = api.state.aqua
        return {"origine": "simulation_actifs", "horizon_jours": 30, "quota_inspection": 0.2,
            "actifs": enregistrements(s.priorites),
            "interpretation": "Indice relatif, pas probabilite de rupture ; ages/categories/historiques inventes.",
            "raccords": "J_A a J_D sont des raccords hypothetiques associes aux noeuds, pas un inventaire reel."}

    @api.post("/api/v1/detection/predire")
    def predire_detection(body: DetectionEntree):
        s = api.state.aqua
        try:
            p = s.detecteur.predire(pd.DataFrame(body.mesures), s.modele_detection)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, "Mesures refusees : " + str(exc)) from None
        return {"origine": "simulation", "sans_etat_persistant": True,
            "interpretation": "Historique du lot seulement ; lissage/persistance recommencent a son premier point.",
            "predictions": enregistrements(p)}

    @api.post("/api/v1/risque/predire")
    def predire_risque(body: RisqueEntree):
        s = api.state.aqua
        try:
            x = s.risque_module.verifier_entrees(pd.DataFrame(body.observations))
            p = s.predire_risque(x)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, "Observations refusees : " + str(exc)) from None
        return {"origine": "simulation_actifs", "horizon_jours": 30, "quota_inspection": 0.2,
            "probabilite_reelle_estimee": False, "priorites": enregistrements(p)}

    return api


def verifier(etat):
    """Controle HTTP et parite avec les modeles, sans relire les verites."""
    from fastapi.testclient import TestClient
    controles = {}
    avant = dict(etat.empreintes)
    with TestClient(creer_app(etat, etat.racine)) as client:
        for path in ["/", "/api/v1/sante", "/api/v1/resume", "/api/v1/scenarios", "/api/v1/priorites", "/openapi.json"]:
            reponse = client.get(path)
            if reponse.status_code != 200:
                raise ValueError("Route en echec : " + path)
        controles["routes_et_page_html"] = True
        if client.get("/api/v1/scenarios/inconnu").status_code != 404:
            raise ValueError("Simulation inconnue non refusee.")
        for s in etat.liste_scenarios:
            j = client.get("/api/v1/scenarios/" + s["scenario_id"]).json()
            p = etat.groupes_predictions[s["scenario_id"]]
            if j["alerte"] != p.alerte.tolist() or j["tuyau_signale"] != p.tuyau_signale.tolist() or len(j["temps_s"]) != len(p):
                raise ValueError("Rejeu different du fichier de prediction.")
        controles["rejeu_parite_tous_scenarios"] = len(etat.liste_scenarios)
        # Un cas normal et un cas contenant une alerte : inference reelle de
        # l'API et comparaison aux decisions sauvegardees, sans labels futurs.
        exemples = [etat.defaut, next(s for s in etat.liste_scenarios if not s["contient_alerte"])]
        for exemple in exemples:
            sid = exemple["scenario_id"]
            x = etat.groupes_capteurs[sid]
            reponse = client.post("/api/v1/detection/predire", json={"origine": "simulation", "mesures": enregistrements(x)})
            if reponse.status_code != 200:
                raise ValueError("Inference detection HTTP en echec : " + reponse.text)
            p = pd.DataFrame(reponse.json()["predictions"])
            attendu = etat.groupes_predictions[sid]
            if not p.alerte.equals(attendu.alerte) or not p.tuyau_signale.equals(attendu.tuyau_signale):
                raise ValueError("Decisions API et V3 differentes.")
        controles["inference_detection_parite_2_journees"] = True
        x = enregistrements(etat.observations_risque)
        reponse = client.post("/api/v1/risque/predire", json={"origine": "simulation_actifs", "observations": x})
        if reponse.status_code != 200 or [r["actif_id"] for r in reponse.json()["priorites"]] != etat.priorites.actif_id.tolist():
            raise ValueError("Classement API et risque V1 differents.")
        controles["inference_risque_parite"] = True
        ajoute_verite = dict(x[0], defaillance_30j=1)
        if client.post("/api/v1/risque/predire", json={"origine": "simulation_actifs", "observations": [ajoute_verite]}).status_code != 422:
            raise ValueError("Verite future acceptee dans les observations.")
        terrain = dict(x[0], origine="terrain")
        if client.post("/api/v1/risque/predire", json={"origine": "simulation_actifs", "observations": [terrain]}).status_code != 422:
            raise ValueError("Donnee reelle acceptee par le prototype risque fictif.")
        if client.post("/api/v1/detection/predire", json={"origine": "terrain", "mesures": [{}]}).status_code != 422:
            raise ValueError("Origine non prise en charge acceptee.")
        if client.post("/api/v1/detection/predire", json={"origine": "simulation", "mesures": []}).status_code != 422:
            raise ValueError("Lot vide accepte.")
        controles["entrees_invalides_et_verite_future_refusees"] = True
    if avant != {f: sha256(f) for f in avant}:
        raise ValueError("Un fichier de modele ou resultat a change.")
    controles["modeles_et_resultats_non_modifies"] = True
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    sortie = etat.racine / "resultats_aqua/api_v1" / identifiant
    sortie.mkdir(parents=True)
    rapport = {"verification": "PASS technique API locale", "controles": controles,
        "origine": "simulation", "validation_terrain": False,
        "modele_detection": str(etat.detection_run), "modele_risque": str(etat.risque_run),
        "sha256_api": sha256(__file__), "sha256_interface": sha256(etat.racine / "aqua_interface_v1.html"),
        "empreintes_modeles_resultats": avant}
    (sortie / "verification.json").write_text(json.dumps(rapport, indent=2, ensure_ascii=False), encoding="utf-8")
    print("API AQUA : PASS technique")
    print("Rejeu verifie :", len(etat.liste_scenarios), "simulations")
    print("Detection et priorites : parite avec les modeles figes")
    print("Donnees : simulations hydrauliques et actifs fictifs ; aucune validation terrain")
    print("Rapport :", sortie / "verification.json")
    return rapport


def ouvrir_quand_pret(port):
    url = f"http://{HOTE}:{port}"
    for _ in range(120):
        try:
            with urlopen(url + "/api/v1/sante", timeout=1) as reponse:
                if reponse.status == 200 and json.load(reponse).get("statut") == "pret":
                    webbrowser.open(url)
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(description="AQUA Sentinel : API et interface locales sur simulations.")
    parser.add_argument("action", choices=["verifier", "servir"])
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--ouvrir", action="store_true", help="Ouvrir le navigateur quand l'API est prete.")
    parser.add_argument("--modele-detection", type=Path)
    parser.add_argument("--modele-risque", type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Le port doit etre entre 1024 et 65535.")
    etat = EtatAqua(DOSSIER, args.modele_detection, args.modele_risque)
    if args.action == "verifier":
        verifier(etat)
        return
    import uvicorn
    with socket.socket() as sonde:
        try:
            sonde.bind((HOTE, args.port))
        except OSError:
            parser.error(f"Port {args.port} deja utilise. Essayer --port {args.port + 1}.")
    if args.ouvrir:
        threading.Thread(target=ouvrir_quand_pret, args=(args.port,), daemon=True).start()
    print(f"Interface : http://{HOTE}:{args.port}")
    print(f"Documentation API : http://{HOTE}:{args.port}/docs")
    print("Mode demonstration : rejeu de simulations ; risque sur actifs fictifs. Ctrl+C pour arreter.")
    uvicorn.run(creer_app(etat, DOSSIER), host=HOTE, port=args.port, log_level="info")


app = creer_app()

if __name__ == "__main__":
    main()
