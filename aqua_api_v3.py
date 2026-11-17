r"""AQUA Sentinel - couche de supervision V3 issue du document de vision.

  .venv\Scripts\python.exe aqua_api_v3.py verifier
  .venv\Scripts\python.exe aqua_api_v3.py servir --ouvrir

V3 est la version de plateforme, distincte du nom historique du detecteur
V3 Bilans_13. Les codes et resultats des versions precedentes sont conserves.
"""
from __future__ import annotations
import argparse
from contextlib import asynccontextmanager
from datetime import datetime
import csv
import html
import io
import json
from pathlib import Path
import socket
import threading
from typing import Literal

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

import aqua_api_v1 as v1
import aqua_api_v2 as v2
from aqua_v2_core import PIPES, utc
from aqua_v3_monitor import Reference, analyze, raw_rows, weekly_profile, digest
from aqua_v3_ops import Operations, POLICY
from aqua_v3_agent import answer
from aqua_v3_acoustique import correlate

ROOT = Path(__file__).resolve().parent
PORT = 8014


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Complement(Contract):
    origine: Literal["simulation"]
    sequence: StrictInt = Field(ge=0, le=1439)
    acoustique_db: dict[str, float] = Field(default_factory=dict)
    reference_acoustique_db: dict[str, float] = Field(default_factory=dict)
    niveau_reservoir_m: float | None = Field(default=None, ge=0, le=100)
    etat_pompe: Literal["marche", "arret", "defaut", "non_fourni"] = "non_fourni"

    @field_validator("acoustique_db", "reference_acoustique_db")
    @classmethod
    def acoustic(cls, value):
        if set(value)-set(PIPES) or any(not 0 <= n <= 160 for n in value.values()):
            raise ValueError("Acoustique : cles P2--P5 et valeurs d'exercice entre 0 et 160 dB.")
        return value


class WeeklySample(Contract):
    horodatage: str = Field(min_length=20,max_length=40)
    conduite: Literal["P2","P3","P4","P5"]
    debit_l_s: float = Field(ge=-100,le=100)
    pression_m: float = Field(ge=-5,le=150)

    @field_validator("horodatage")
    @classmethod
    def timestamp(cls,value):
        dt = datetime.fromisoformat(value.replace("Z","+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Fuseau explicite requis.")
        return value


class WeeklyInput(Contract):
    origine: Literal["simulation"]
    references: list[WeeklySample] = Field(min_length=1,max_length=10080)


class ContextInput(Contract):
    origine: Literal["simulation"]
    version: StrictInt = Field(ge=0)
    charge_source_m: float = Field(default=55, ge=15, le=90)
    demande_A_l_s: float = Field(default=6, ge=1, le=15)
    demande_B_l_s: float = Field(default=4, ge=1, le=15)
    hypothese_fuite_zone: Literal["","A","B"] = ""
    hypothese_fuite_mm: float = Field(default=0,ge=0,le=12)
    qualite_capteurs: StrictBool = True
    communication: StrictBool = True
    mode: Literal["manuel","assiste","automatique"] = "assiste"
    usagers_A: StrictInt | None = Field(default=None,ge=0,le=1000000)
    usagers_B: StrictInt | None = Field(default=None,ge=0,le=1000000)


class ProposalInput(Contract):
    origine: Literal["simulation"]
    action: Literal["equilibrer","reduire_A","reduire_B","isoler_A","isoler_B","retablir"]
    autorisation_critique: StrictBool = False
    justification: str = Field(default="",max_length=1000)


class ExecuteInput(Contract):
    origine: Literal["simulation"]
    approuver: StrictBool


class VerifyInput(Contract):
    origine: Literal["simulation"]
    defaut: Literal["aucun","vanne_bloquee","capteur_absent","reponse_lente"] = "aucun"


class OrderInput(Contract):
    origine: Literal["simulation"]
    session_id: str = Field(min_length=1,max_length=80)
    conduite: Literal["P2","P3","P4","P5"]
    responsable: str = Field(min_length=1,max_length=120)
    description: str = Field(min_length=5,max_length=2000)
    echeance: str | None = None
    cle: str = Field(min_length=8,max_length=100)

    @field_validator("echeance")
    @classmethod
    def date(cls,value):
        if value:
            from datetime import date
            date.fromisoformat(value)
        return value


class OrderUpdate(Contract):
    version: StrictInt = Field(ge=0)
    statut: Literal["en_cours","a_verifier","cloturee","annulee"]
    note: str = Field(min_length=3,max_length=2000)


class AgentInput(Contract):
    session_id: str = Field(min_length=1,max_length=80)
    question: str = Field(min_length=3,max_length=1500)


class AcousticInput(Contract):
    origine: Literal["simulation"]
    session_id: str = Field(min_length=1,max_length=80)
    sequence: StrictInt = Field(ge=0,le=1439)
    conduite: Literal["P2","P3","P4","P5"]
    frequence_hz: float = Field(ge=10,le=200000)
    distance_capteurs_m: float = Field(gt=0,le=10000)
    vitesse_onde_m_s: float = Field(ge=50,le=2000)
    synchronisation_confirmee: StrictBool
    amont: list[float] = Field(min_length=32,max_length=4096)
    aval: list[float] = Field(min_length=32,max_length=4096)


def report_html(data):
    esc = lambda x: html.escape(str(x))
    s = data["analyse"]
    headings = ["Secteur","Etat","Gravite","Preuves / couverture","Bilan L/s","Volume estime m3","Pression mCE","Conseil"]
    table = ''.join('<tr>'+''.join('<td>'+esc(x)+'</td>' for x in [r["id"],r["etat"],r["gravite"],
        f"{r['score_preuves_0_100']} / {r['couverture_preuves_sur_100']}",r["bilan_l_s"],r["volume_estime_m3"],r["pression_m"],r["recommandation"]])+'</tr>' for r in s["secteurs"])
    orders = ''.join('<li>'+esc(o["conduite"]+' - '+o["statut"]+' - '+o["responsable"]+' : '+o["description"])+'</li>' for o in data["interventions"])
    return '<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>AQUA - rapport de session</title><style>body{font:14px/1.5 Arial;color:#173d34;max-width:1100px;margin:40px auto;padding:0 20px}table{border-collapse:collapse;width:100%;font-size:11px}td,th{border:1px solid #ccc;padding:8px;text-align:left}pre{white-space:pre-wrap;font-size:10px;overflow-wrap:anywhere}h1{font-size:28px}@media print{button{display:none}body{margin:0}}</style><h1>AQUA Sentinel - rapport de supervision</h1><p><strong>Simulation uniquement - aucune validation terrain</strong></p><p>Export UTC : '+esc(data["export_utc"])+'. Session : '+esc(s["session_id"])+'. Sequence recue : '+esc(s["sequence"])+'. Temps relatif : '+esc(s["temps_s"])+ ' s.</p><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headings)+'</tr></thead><tbody>'+table+'</tbody></table><h2>Interventions</h2><ul>'+(orders or '<li>Aucune intervention pour cette session.</li>')+'</ul><h2>Limites et provenance</h2><p>'+esc(s["limite"])+'</p><p>Les volumes concernent uniquement cette session, pas une journee civile ni le reseau reel. Les anciens scores V3 ne valident pas les ajouts de cette plateforme.</p><pre>'+esc(json.dumps({"reference":s["reference"],"empreinte_snapshot":data["sha256_snapshot"],"journal_intact":data["audit_intact"]},ensure_ascii=False,indent=2))+'</pre><p>Sources : mesures SQLite de la session ; references train indiquees ci-dessus ; codes AQUA V3. Modele hydraulique : <a href="https://usepa.github.io/WNTR/hydraulics.html">WNTR / US EPA</a>.</p><button onclick="window.print()">Imprimer / enregistrer en PDF</button></html>'


def creer_app(etat=None, racine=ROOT, db_path=None, v2_db=None, worker=True):
    racine = Path(racine)
    api = v2.creer_app(etat,racine,v2_db,worker)
    previous = api.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            application.state.operations = Operations(db_path or racine/"resultats_aqua/plateforme_v3/aqua_v3.sqlite3")
            application.state.reference = Reference(application.state.aqua)
            yield
    api.router.lifespan_context = lifespan
    api.title, api.version = "AQUA Sentinel - Plateforme V3", "3.0"
    api.router.routes = [r for r in api.router.routes if getattr(r,"path",None) != "/"]

    def invoke(fn,*args,**kw):
        try:
            return fn(*args,**kw)
        except KeyError as e:
            raise HTTPException(404,str(e)) from None
        except ValueError as e:
            raise HTTPException(422,str(e)) from None

    def snapshot(sid):
        ops = api.state.operations
        s = invoke(analyze,api.state.v2.store,api.state.reference,sid,ops.get("complements:"+sid,{}),ops.get("hebdomadaire"))
        orders = [o for o in ops.orders() if o["session_id"] == sid]
        s["interventions_ouvertes"] = sum(o["statut"] not in ("cloturee","annulee") for o in orders)
        return s

    @api.get("/",include_in_schema=False)
    def page():
        return FileResponse(racine/"aqua_interface_v3.html",media_type="text/html; charset=utf-8")

    @api.get("/v2",include_in_schema=False)
    def legacy():
        return FileResponse(racine/"aqua_interface_v2.html",media_type="text/html; charset=utf-8")

    @api.get("/assets/aqua_v3.js",include_in_schema=False)
    def script():
        return FileResponse(racine/"aqua_interface_v3.js",media_type="text/javascript; charset=utf-8")

    @api.get("/api/v3/sante")
    def health():
        return {"version":"3.0","origine":"simulation","commande_materielle":False,"validation_terrain":False,
                "modules":["supervision","secteurs_pilotes","baselines_train","references_hebdomadaires_importees","minimum_nocturne",
                           "preuves_multisignal","securite_deterministe","verification_post_action","interventions_suivies","assistant_source","rapports"],
                "detecteur_historique":"Bilans_13 inchange","politique_laboratoire":POLICY,
                "assistant":"Local a regles par defaut ; selection des preuves par LLM Groq si configure explicitement.",
                "materiel_absent":["debitmetres reels","acoustique reelle","actionneurs reels","historique de casses terrain","geolocalisation acoustique fine"]}

    @api.get("/api/v3/sessions/{sid}/analyse")
    def analysis(sid:str):
        return snapshot(sid)

    @api.get("/api/v3/sessions/{sid}/mesures/{sequence}")
    def evidence(sid:str,sequence:int):
        store = api.state.v2.store
        invoke(store.session,sid)
        with store.connection() as c:
            r = c.execute("SELECT entree_json,sortie_json FROM mesures WHERE session_id=? AND sequence=?",(sid,sequence)).fetchone()
        if r is None:
            raise HTTPException(404,"Mesure non recue.")
        return {"entree":json.loads(r[0]),"sortie":json.loads(r[1]),
                "complement":api.state.operations.get("complements:"+sid,{}).get(str(sequence))}

    @api.post("/api/v3/sessions/{sid}/complements")
    def complement(sid:str,body:Complement):
        store = api.state.v2.store
        s = invoke(store.session,sid)
        if body.sequence >= s["sequence"]:
            raise HTTPException(422,"La mesure correspondante doit deja avoir ete recue ; aucun complement futur.")
        return invoke(api.state.operations.complement,sid,body.sequence,body.model_dump(exclude={"origine","sequence"}))

    @api.get("/api/v3/baselines/{pipe}")
    def baseline(pipe:str):
        if pipe not in PIPES:
            raise HTTPException(404,"Secteur pilote inconnu.")
        return {"conduite":pipe,"reference":api.state.reference.info,"quotidien":api.state.reference.export(pipe),
                "hebdomadaire":api.state.operations.get("hebdomadaire",{"disponible":False})}

    @api.post("/api/v3/acoustique/localiser")
    def acoustic(body:AcousticInput):
        s = invoke(api.state.v2.store.session,body.session_id)
        if body.sequence >= s["sequence"]:
            raise HTTPException(422,"Aucune mesure recue pour cette sequence.")
        result = invoke(correlate,body.model_dump())
        api.state.operations.put("acoustique:"+body.session_id+":"+str(body.sequence),result)
        return result

    @api.post("/api/v3/baselines/hebdomadaire")
    def weekly(body:WeeklyInput):
        profile = invoke(weekly_profile,[p.model_dump() for p in body.references])
        return api.state.operations.put("hebdomadaire",profile)

    @api.get("/api/v3/controle/contexte")
    def context():
        return api.state.operations.context()

    @api.put("/api/v3/controle/contexte")
    def context_update(body:ContextInput):
        if bool(body.hypothese_fuite_zone) != bool(body.hypothese_fuite_mm):
            raise HTTPException(422,"Une hypothese de fuite exige une zone et un diametre non nul ensemble.")
        return invoke(api.state.operations.set_context,body.model_dump(exclude={"origine","version"}),body.version)

    @api.get("/api/v3/controle/propositions")
    def proposals():
        return {"propositions":api.state.operations.proposals()}

    @api.post("/api/v3/controle/propositions",status_code=201)
    def propose(body:ProposalInput):
        return invoke(api.state.operations.propose,body.action,body.autorisation_critique,body.justification)

    @api.post("/api/v3/controle/propositions/{identity}/executer")
    def execute(identity:str,body:ExecuteInput):
        return invoke(api.state.operations.execute,identity,body.approuver)

    @api.post("/api/v3/controle/propositions/{identity}/verifier")
    def verify(identity:str,body:VerifyInput):
        return invoke(api.state.operations.verify,identity,body.defaut)

    @api.post("/api/v3/controle/cycle_automatique")
    def auto_cycle(body:ExecuteInput):
        ops = api.state.operations
        with ops.lock:
            if ops.get("contexte")["mode"] != "automatique" or not body.approuver:
                raise HTTPException(422,"Choisir le mode automatique et lancer explicitement un cycle de laboratoire.")
            p = invoke(ops.propose,"equilibrer")
            if not p["evaluation"]["autorisee"]:
                return p
            invoke(ops.execute,p["id"],True)
            return invoke(ops.verify,p["id"],"aucun")

    @api.get("/api/v3/interventions")
    def orders(session_id:str|None=None):
        return {"interventions":[o for o in api.state.operations.orders() if session_id is None or o["session_id"] == session_id]}

    @api.post("/api/v3/interventions",status_code=201)
    def create_order(body:OrderInput):
        snap = snapshot(body.session_id)
        if not snap["sequence"]:
            raise HTTPException(422,"Recevoir une mesure avant de planifier une intervention.")
        return invoke(api.state.operations.create_order,body.model_dump(exclude={"cle","origine"}),snap,body.cle)

    @api.patch("/api/v3/interventions/{identity}")
    def update_order(identity:str,body:OrderUpdate):
        item = invoke(api.state.operations.order,identity)
        sid, p = item["session_id"], item["conduite"]
        snap = snapshot(sid)
        rows = raw_rows(api.state.v2.store,sid)
        after = [r for r in rows if r["sequence"] >= item.get("sequence_a_verifier", item["sequence_creation"])]
        clean = len(after) >= 3 and all(r["sortie"]["paires"][p]["etat"] == "pas_de_declenchement" and not r["sortie"]["paires"][p]["alerte_memorisee"] for r in after[-3:])
        return invoke(api.state.operations.update_order,identity,body.statut,body.note,body.version,snap,clean)

    @api.get("/api/v3/audit")
    def audit():
        return api.state.operations.audit()

    @api.post("/api/v3/assistant")
    def assistant(body:AgentInput):
        snap = snapshot(body.session_id)
        if not snap["sequence"]:
            raise HTTPException(422,"Aucune mesure recue pour repondre sur cette session.")
        priorities = v1.enregistrements(api.state.aqua.priorites)
        scoped = [o for o in api.state.operations.orders() if o["session_id"] == body.session_id]
        result = answer(body.question,snap,scoped,priorities,api.state.operations.get("contexte"))
        identity = digest([body.session_id,body.question,snap["sequence"],result])
        result["reponse_id"] = identity
        for fact in result["faits"]:
            if fact["source"]["type"] == "simulation_sans_execution":
                fact["source"]["url"] = "/api/v3/assistant/reponses/"+identity
        api.state.operations.put("assistant:"+identity,result)
        return result

    @api.get("/api/v3/assistant/reponses/{identity}")
    def saved_answer(identity:str):
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise HTTPException(404,"Reponse inconnue.")
        value = api.state.operations.get("assistant:"+identity)
        if value is None:
            raise HTTPException(404,"Reponse inconnue.")
        return value

    def export_data(sid):
        snap = snapshot(sid)
        return {"version":"3.0","export_utc":utc(),"analyse":snap,
                "sha256_snapshot":digest(snap),"interventions":[o for o in api.state.operations.orders() if o["session_id"]==sid],
                "audit_intact":api.state.operations.audit()["integrite_chaine"],
                "sources":["Mesures et sorties causales de cette session","Train sain pour les references horaires","Completements de simulation explicitement declares"]}

    @api.get("/api/v3/sessions/{sid}/rapport")
    def report(sid:str,format:Literal["json","html","csv"]="json"):
        data = export_data(sid)
        if format == "json":
            return data
        if format == "html":
            return HTMLResponse(report_html(data),headers={"Content-Disposition":"attachment; filename=AQUA_rapport_session.html"})
        buffer = io.StringIO(newline="")
        fields = ["id","etat","gravite","pression_m","debit_entree_l_s","bilan_l_s","volume_estime_m3","score_preuves_0_100","couverture_preuves_sur_100"]
        writer = csv.DictWriter(buffer,fieldnames=fields,extrasaction="ignore")
        writer.writeheader();writer.writerows(data["analyse"]["secteurs"])
        return Response("\ufeff"+buffer.getvalue(),media_type="text/csv; charset=utf-8",headers={"Content-Disposition":"attachment; filename=AQUA_rapport_secteurs.csv"})

    return api


def main():
    parser = argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action",choices=["verifier","servir"])
    parser.add_argument("--port",type=int,default=PORT)
    parser.add_argument("--ouvrir",action="store_true")
    parser.add_argument("--base",type=Path,help="Base operations V3.")
    parser.add_argument("--base-v2",type=Path,help="Base du flux V2, conservee par defaut.")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port entre 1024 et 65535 requis.")
    base = v1.EtatAqua(ROOT)
    if args.action == "verifier":
        from aqua_verifier_plateforme_v3 import verifier
        verifier(base)
        return
    for name in ["aqua_interface_v3.html","aqua_interface_v3.js"]:
        if not (ROOT/name).is_file():
            parser.error("Fichier absent : "+name)
    with socket.socket() as probe:
        try:
            probe.bind((v1.HOTE,args.port))
        except OSError:
            parser.error(f"Port occupe ; utiliser --port {args.port+1}.")
    if args.ouvrir:
        threading.Thread(target=v1.ouvrir_quand_pret,args=(args.port,),daemon=True).start()
    import uvicorn
    print(f"AQUA Sentinel Plateforme V3 : http://{v1.HOTE}:{args.port}")
    print("Simulation ; conserver cette fenetre ouverte. Ctrl+C pour arreter.")
    uvicorn.run(creer_app(base,ROOT,args.base,args.base_v2),host=v1.HOTE,port=args.port)


if __name__ == "__main__":
    main()
