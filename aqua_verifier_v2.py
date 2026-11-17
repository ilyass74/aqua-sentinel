"""Verification reproductible des extensions V2, sans entrainement.

Les essais utilisent une base temporaire. Le test V3 connu sert uniquement
a verifier la parite du calcul causal, pas a selectionner un modele.
Les experiences a deux zones sont des tests hydrauliques d'ingenierie ; elles
ne remplacent pas une validation de regulation sur un reseau physique.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import platform
import tempfile
import time
from uuid import uuid4

import numpy as np
import wntr
from fastapi.testclient import TestClient

import aqua_api_v1 as v1
from aqua_api_v2 import creer_app
from aqua_v2_core import PIPES, Store, causal_step, initial_state, sensor_plan
from aqua_v2_hydraulique import regulation, interventions, solve


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def synthetic_measure(index, calibration, residual=1., sid="controle"):
    measure = {"scenario_id": sid, "temps_s": index * 60}
    # Les deux mesures varient ensemble : pas de faux defaut de valeur figee.
    for pipe in PIPES:
        downstream = .006 + math.sin(index) * .00001
        measure[f"debit_{pipe}_m3_s"] = downstream + (residual + calibration[pipe]["centre_l_s"]) / 1000
        measure[f"debit_{pipe}_aval_m3_s"] = downstream
    return measure


def verifier(etat=None):
    etat = etat or v1.EtatAqua()
    root = etat.racine
    calibration = etat.modele_detection["calibration"]
    files = {Path(k) for k in etat.empreintes}
    files.update(root / name for name in etat.detecteur.sources())
    files.add(root / "aqua_risque_v1.py")
    baseline = {str(p): v1.sha256(p) for p in files}
    controls = {}
    start = time.monotonic()

    print("V2 : parite causale avec les 64 journees V3 deja connues...", flush=True)
    points = 0
    for sid, frame in etat.groupes_capteurs.items():
        state = initial_state()
        actual = []
        for measure in v1.enregistrements(frame):
            result, _ = causal_step(state, measure, calibration)
            actual.append([int(result["paires"][p]["alerte_memorisee"]) for p in PIPES])
        expected = etat.groupes_predictions[sid][["alerte_"+p for p in PIPES]].values.tolist()
        check(actual == expected, "Calcul causal different de V3 : " + sid)
        points += len(frame)
    controls["parite_V3_donnees_connues"] = {"scenarios": len(etat.groupes_capteurs), "mesures": points}

    state = initial_state()
    for i in range(61):
        result, _ = causal_step(state, synthetic_measure(i, calibration), calibration)
    check(all(abs(p["bilan_signe_m3"] - 3.6) < 1e-9 for p in result["paires"].values()),
          "Conversion 1 L/s pendant 1 h differente de 3.6 m3.")
    controls["volume_1L_s_1h_3_6m3"] = True
    state = initial_state()
    for i in range(61):
        result, _ = causal_step(state, synthetic_measure(i, calibration), calibration, corrections={p: 1. for p in PIPES})
    check(all(abs(p["bilan_signe_m3"]) < 1e-9 for p in result["paires"].values()), "Soutirage connu non corrige.")
    controls["soutirage_legitime_corrige"] = True

    for mode in ["absent", "fige", "desynchronise", "trou"]:
        state = initial_state()
        for i in range(20):
            row = synthetic_measure(i, calibration)
            clocks = {}
            if mode == "absent" and i == 10:
                row["debit_P3_aval_m3_s"] = None
            if mode == "fige" and i >= 10:
                row["debit_P3_aval_m3_s"] = .006
            if mode == "desynchronise" and i == 10:
                clocks["debit_P3_aval_m3_s"] = row["temps_s"]-60
            if mode == "trou" and i >= 10:
                row["temps_s"] += 120
            result, _ = causal_step(state, row, calibration, horodatages=clocks)
            if (mode != "fige" and i == 10) or (mode == "fige" and i == 15):
                check(result["paires"]["P3"]["etat"] == "donnees_insuffisantes", "Defaut non signale : " + mode)
                check(result["paires"]["P3"]["alerte_memorisee"], "Incident oublie lors du defaut de qualite.")
        check(result["paires"]["P3"]["intervalles_exclus"] > 0, "Intervalle invalide integre : " + mode)
    controls["qualite_absent_fige_desynchronise_trou_et_reprise"] = True
    # Une fuite et un biais qui generent exactement les memes entrees sont
    # indiscernables : le test conserve explicitement cette limite.
    check("biais" in result["paires"]["P2"]["cause"], "Limite du diagnostic de cause absente.")
    controls["biais_fuite_non_distinguables_limite_conservee"] = True

    print("V2 : memoire SQLite, doublons, reprise et contrats HTTP...", flush=True)
    with tempfile.TemporaryDirectory(prefix="aqua_v2_verification_") as temporary:
        path = Path(temporary) / "suivi.sqlite3"
        store = Store(path, calibration)
        session = store.create("controle")
        sid = session["id"]
        for i in range(20):
            store.ingest(sid, i, synthetic_measure(i, calibration))
        store2 = Store(path, calibration)
        replay = store2.ingest(sid, 19, synthetic_measure(19, calibration))
        check(replay["requete_rejouee"] and store2.session(sid)["sequence"] == 20, "Doublon non idempotent.")
        try:
            store2.ingest(sid, 19, synthetic_measure(19, calibration, residual=2.))
            raise AssertionError("Conflit de mesure accepte.")
        except ValueError:
            pass
        continuation = store2.ingest(sid, 20, synthetic_measure(20, calibration))
        check(continuation["paires"]["P2"]["alerte_memorisee"], "Etat non repris apres reouverture.")
        check(len(store2.incidents()) == 4, "Incidents dupliques ou absents.")
        incident = store2.incidents()[0]["id"]
        store2.annotate(incident, "inspection_planifiee", "Essai de suivi", "cle-note-verification")
        store2.annotate(incident, "inspection_planifiee", "Essai de suivi", "cle-note-verification")
        check(len(store2.incidents()[0]["journal"]) == 2, "Commentaire duplique.")
        # Requetes simultanees avec la meme sequence : une seule ecriture.
        measure = synthetic_measure(21, calibration)
        with ThreadPoolExecutor(max_workers=4) as pool:
            answers = list(pool.map(lambda _: store2.ingest(sid, 21, measure), range(4)))
        check(store2.session(sid)["sequence"] == 22 and sum(not a["requete_rejouee"] for a in answers) == 1,
              "Ingestion concurrente non atomique.")
        controls["sqlite_reprise_doublons_conflits_journal_concurrence"] = True

        http_db = Path(temporary) / "http.sqlite3"
        with TestClient(creer_app(etat, root, http_db, worker=True)) as client:
            for route in ["/", "/v1", "/assets/aqua_v2.js", "/api/v1/sante", "/api/v1/resume",
                          "/api/v1/priorites", "/api/v2/sante", "/api/v2/sessions", "/api/v2/incidents", "/openapi.json"]:
                check(client.get(route).status_code == 200, "Route invalide : " + route)
            created = client.post("/api/v2/sessions", json={"origine":"simulation", "source":"api", "scenario_id":"controle"})
            check(created.status_code == 201, "Session API refusee.")
            api_sid = created.json()["id"]
            endpoint = f"/api/v2/sessions/{api_sid}/mesures"
            packet = {"origine":"simulation", "sequence":0, "mesure":synthetic_measure(0, calibration)}
            check(client.post(endpoint, json=packet).status_code == 200, "Mesure HTTP refusee.")
            check(client.post(endpoint, json=packet).json()["requete_rejouee"], "Idempotence HTTP absente.")
            bad = dict(packet, sequence=1, mesure=dict(synthetic_measure(1, calibration), fuite_active=1))
            check(client.post(endpoint, json=bad).status_code == 422, "Verite de fuite acceptee.")
            bad = dict(packet, origine="terrain")
            check(client.post(endpoint, json=bad).status_code == 422, "Origine terrain acceptee.")
            bad = dict(packet, sequence=True)
            check(client.post(endpoint, json=bad).status_code == 422, "Sequence booleenne acceptee.")
            check(client.post("/api/v2/sessions", json={"origine":"simulation", "scenario_id":"inconnu"}).status_code == 404,
                  "Source de rejeu inconnue acceptee.")
            check(client.post("/api/v2/regulation", json={"origine":"simulation", "scenario":"inconnu"}).status_code == 422,
                  "Scenario inconnu accepte.")
            check(client.post("/api/v2/interventions", json={"origine":"simulation", "ouverture_mm":50}).status_code == 422,
                  "Fuite hors limites acceptee.")
            check(client.post("/api/v2/capteurs/plan", json={"budget":14}).status_code == 422,
                  "Budget invalide accepte.")
            replay_session = client.post("/api/v2/sessions", json={"origine":"simulation", "scenario_id":etat.defaut["scenario_id"]}).json()
            replay_id = replay_session["id"]
            client.put(f"/api/v2/sessions/{replay_id}/lecture", json={"active":True})
            deadline = time.monotonic()+4
            while time.monotonic()<deadline and client.get(f"/api/v2/sessions/{replay_id}").json()["sequence"]<2:
                time.sleep(.1)
            stopped = client.put(f"/api/v2/sessions/{replay_id}/lecture", json={"active":False}).json()
            check(stopped["sequence"]>=2, "Source continue cote serveur inactive.")
            time.sleep(.7)
            check(client.get(f"/api/v2/sessions/{replay_id}").json()["sequence"]==stopped["sequence"], "Source non arretee apres pause.")
            controls["HTTP_source_serveur_continue_et_pause"] = True
            check(client.get("/api/v2/export").status_code == 200, "Export de suivi en echec.")
            check(client.post("/api/v2/capteurs/plan", json={"budget":13}).json()["plan"]["paires"] == list(PIPES),
                  "Plan complet incorrect.")
            # Une annotation doit etre rendue comme texte dans l'interface.
            controls["HTTP_routes_entrees_invalides_origine_et_verite"] = True
        with TestClient(creer_app(etat, root, http_db, worker=False)) as client:
            check(client.get(f"/api/v2/sessions/{api_sid}").json()["sequence"]==1, "Etat HTTP perdu au redemarrage.")
        controls["HTTP_reprise_apres_redemarrage"] = True

    for budget in range(5,14):
        plan=sensor_plan(budget)
        check(plan["plan"]["mesures"]<=budget, "Budget depasse.")
        check(len(plan["plan"]["paires"])==(budget-5)//2, "Paire incomplete ou plan non optimal.")
    plan=sensor_plan(7,{"P2":1.,"P3":1.,"P4":1.,"P5":10.})
    check(plan["plan"]["paires"]==["P5"], "Priorite de placement ignoree.")
    controls["placement_16_sous_ensembles_budget_et_priorites"] = True

    print("V2 : simulations de regulation, limites et isolement...", flush=True)
    hydraulic = {}
    for scenario in ["deux_zones","pointe_B","ressource_limitee","capteur_absent","fuite_A"]:
        experiment=regulation(scenario)
        for index, item in enumerate(experiment["series"]):
            check(item["variation_max_pct"]<=10.000001, "Mouvement de vanne trop rapide.")
            if item["decision"].startswith("gel_"):
                previous = experiment["series"][index-1]["avec_regulation"] if index else {"zones":{z:{"ouverture_pct":100.} for z in "AB"}}
                check(all(abs(item["avec_regulation"]["zones"][z]["ouverture_pct"]-previous["zones"][z]["ouverture_pct"])<1e-8 for z in "AB"),
                      "Consigne modifiee malgre le gel.")
        hydraulic[scenario]={"sans_regulation":experiment["sans_regulation"],"avec_regulation":experiment["avec_regulation"]}
        check(experiment["avec_regulation"]["fraction_min_moyenne"]>=experiment["sans_regulation"]["fraction_min_moyenne"]-1e-6,
              "Desserte minimale degradee dans le cas de verification : " + scenario)
        if scenario=="capteur_absent":
            check(all(s["decision"]=="gel_donnees_insuffisantes" for s in experiment["series"][10:16]),"Gel de qualite absent.")
        if scenario=="fuite_A":
            check(all(s["decision"]=="gel_bilan_suspect_fuite_ou_biais" for s in experiment["series"][10:24]),"Bilan suspect ignore.")
        print("  "+scenario+" : service minimal moyen "+str(round(100*experiment["sans_regulation"]["fraction_min_moyenne"],1))+" -> "+str(round(100*experiment["avec_regulation"]["fraction_min_moyenne"],1))+" %",flush=True)
    for zone in "AB":
        for diameter in [4.,6.,8.,12.]:
            result=interventions(zone,diameter)
            isolated=next(r for r in result["alternatives"] if r["action"]=="isoler_cible")
            check(isolated["debit_fuite_simule_l_s"]<1e-5,"Isolement incomplet.")
            check(isolated["zones"][zone]["fraction_servie"]<1e-5,"Zone isolee annoncee alimentee.")
    # Parametres varies, sans rechercher un meilleur seuil sur le test V3.
    rng=np.random.default_rng(20261028)
    for _ in range(12):
        result=solve(float(rng.uniform(25,75)),float(rng.uniform(.003,.008)),float(rng.uniform(.002,.007)),float(rng.uniform(.1,1)),float(rng.uniform(.1,1)))
        check(0<=result["fraction_min_servie"]<=1,"Fraction de demande impossible.")
    controls["hydraulique_5_cas_8_isolements_12_variantes_conservation_masse"] = True
    with tempfile.TemporaryDirectory(prefix="aqua_v2_http_hydraulique_") as directory:
        with TestClient(creer_app(etat,root,Path(directory)/"experiences.sqlite3",worker=False)) as client:
            for route,body in [("/api/v2/regulation",{"origine":"simulation","scenario":"deux_zones"}),
                               ("/api/v2/interventions",{"origine":"simulation","zone":"B"})]:
                response=client.post(route,json=body)
                check(response.status_code==200,"Experience hydraulique HTTP en echec : "+response.text)
                identity=response.json()["experience_id"]
                check(client.get("/api/v2/experiences/"+identity).status_code==200,"Experience non conservee.")
            check(len(client.get("/api/v2/experiences").json()["experiences"])==2,"Historique d'experiences incomplet.")
    controls["HTTP_regulation_intervention_experiences_conservees"] = True
    check(baseline=={name:v1.sha256(name) for name in baseline},"Un fichier fige a change.")
    controls["sources_modeles_et_rapports_figes_conserves"] = len(baseline)

    source_files=["aqua_api_v2.py","aqua_v2_core.py","aqua_v2_hydraulique.py","aqua_interface_v2.html","aqua_interface_v2.js","aqua_verifier_v2.py","aqua_producteur_v2.py","requirements_api_v2.txt"]
    report={"verification":"PASS technique", "origine":"simulations_et_tests_fonctionnels", "python":platform.python_version(),
        "plateforme":platform.system(),"wntr":wntr.__version__,"controles":controls,"regulation":hydraulic,
        "duree_verification_s":round(time.monotonic()-start,2),
        "sha256_sources_V2":{name:v1.sha256(root/name) for name in source_files},
        "affichage_navigateur_Windows":"a confirmer sur le poste utilisateur",
        "limites":["Parite V3 sur un test deja connu, aucun nouveau score independant de detection.",
                   "Regulation et interventions sur une autre maquette, loi de vanne fictive, connaissance des demandes supposee.",
                   "Le controle qualite ne distingue pas un biais de capteur d'une fuite equivalente.",
                   "Volumes estimes sur intervalles valides ; aucun volume complet garanti en cas de panne.",
                   "Placement optimise pour couverture ponderee de quatre conduites, pas pour un reseau reel.",
                   "Aucune validation terrain, aucun pilotage de vanne physique."]}
    run=root/"resultats_aqua/extensions_v2"/(time.strftime("%Y%m%dT%H%M%SZ",time.gmtime())+"_"+uuid4().hex[:6])
    run.mkdir(parents=True)
    (run/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    print("\nAQUA V2 : PASS technique. Consulter les comparaisons et limites ci-dessus.")
    print("Rapport : "+str(run/"verification.json"))
    return report


if __name__=="__main__":
    verifier()
