"""Verification des ajouts de plateforme, pas un nouveau test de qualite ML.

Utilise des bases temporaires et des situations artificielles de controle.
Ne modifie ni les jeux de donnees, ni les modeles, ni les bases de suivi.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from aqua_api_v1 import EtatAqua, enregistrements, sha256
from aqua_v2_core import Store, PIPES
from aqua_v3_monitor import Reference, analyze, weekly_profile, digest
from aqua_v3_ops import Operations, evaluate, default_context
from aqua_v3_agent import answer


def check(condition, text):
    if not condition:
        raise AssertionError(text)


def refuses(function, text):
    try:
        function()
    except (ValueError,KeyError):
        return
    raise AssertionError(text)


def measure(i, reference, calibration, sid="controle", start=0, residual=0., consumption=0.):
    t = start+i*60
    m = {"scenario_id":sid,"temps_s":t}
    for j,p in enumerate(PIPES):
        ref = reference.get(p,t,"debit")
        q = (ref["centre"] if ref else 1.)+.003*math.sin(i*.71+j)
        if p == "P2":
            q += consumption
        m[f"debit_{p}_m3_s"] = q/1000
        m[f"debit_{p}_aval_m3_s"] = (q-calibration[p]["centre_l_s"]-(residual if p == "P2" else 0.))/1000
    for j,n in enumerate("ABCD"):
        m[f"pression_{n}_m"] = 38+.003*math.sin(i*.53+j)
    return m


def verifier(base=None):
    base = base or EtatAqua()
    root = base.racine
    started = time.monotonic()
    report = {"verification":"EN_COURS","python":platform.python_version(),"systeme":platform.system(),"controles":{}}
    controls = report["controles"]
    frozen = {str(p):sha256(p) for p in [root/n for n in base.detecteur.sources()]+[root/"aqua_api_v1.py",root/"aqua_api_v2.py",root/"aqua_v2_core.py",root/"aqua_v2_hydraulique.py",root/"aqua_risque_v1.py"]}
    frozen.update(base.empreintes)
    print("PLATEFORME V3 : references train et calculs explicatifs...",flush=True)
    reference = Reference(base)
    check(reference.info["disponible"] and reference.info["scenarios"] > 0,"Reference train saine indisponible.")
    check(reference.info["jeu"].startswith("train"),"Reference calculee hors train.")
    controls["baselines_train_sain_pas_test_ni_calendrier_invente"] = reference.info
    calibration = base.modele_detection["calibration"]
    with tempfile.TemporaryDirectory(prefix="aqua_plateforme_v3_") as tmp:
        tmp = Path(tmp)
        store = Store(tmp/"mesures.db",calibration)
        session = store.create("controle")
        sid = session["id"]
        empty = analyze(store,reference,sid)
        check(empty["kpi"]["sante_surveillee_pct"] is None,"Sante inventee sans mesure.")
        for i in range(20):
            store.ingest(sid,i,measure(i,reference,calibration,residual=1.))
        snap = analyze(store,reference,sid)
        p2 = next(s for s in snap["secteurs"] if s["id"] == "P2")
        check(p2["alerte_detecteur_fige"] and p2["couverture_preuves_sur_100"] == 80,"Bilan non conserve ou acoustique absente compte comme disponible.")
        check(p2["probabilite_fuite"] is None,"Indice converti en probabilite.")
        check(p2["premier_signal_recu_s"] is not None,"Debut recu non trace.")
        ext = {"19":{"acoustique_db":{"P2":65.},"reference_acoustique_db":{"P2":45.},"niveau_reservoir_m":3.2,"etat_pompe":"arret"}}
        enriched = analyze(store,reference,sid,ext)
        ep2 = next(s for s in enriched["secteurs"] if s["id"] == "P2")
        check(ep2["couverture_preuves_sur_100"] == 100 and abs(ep2["score_preuves_0_100"]-p2["score_preuves_0_100"]-20) < 1e-8,"Acoustique explicite non integree selon les poids.")
        check(enriched["equipements"]["niveau_reservoir_m"] == 3.2,"Niveau perdu.")
        controls["preuves_couverture_absence_acoustique_pression_et_gravite_separees"] = True

        # Meme bilan appaire normal, consommation de nuit accrue : le signal
        # nocturne existe, sans transformer cette consommation en fuite V3.
        night = store.create("nocturne")["id"]
        for i in range(20):
            ref = reference.get("P2",3600+i*60,"debit")
            store.ingest(night,i,measure(i,reference,calibration,sid="nocturne",start=3600,
                                       consumption=4*ref["dispersion"]+1.))
        ns = analyze(store,reference,night)
        n2 = next(s for s in ns["secteurs"] if s["id"] == "P2")
        check(n2["nocturne"]["alerte"] and not n2["alerte_detecteur_fige"],"Consommation nocturne confondue avec la decision du detecteur.")
        check(not n2["nocturne"]["nuit_complete"] and n2["nocturne"]["mesures_disponibles"] == 20,"Nuit incomplete annoncee complete.")
        controls["minimum_nocturne_causal_persistance_et_consommation_sans_fuite"] = True

        missing = measure(20,reference,calibration,residual=1.)
        missing["debit_P2_aval_m3_s"] = None
        store.ingest(sid,20,missing)
        uncertain = analyze(store,reference,sid)
        check(uncertain["secteurs"][0]["etat"] == "indetermine" and uncertain["secteurs"][0]["alerte_detecteur_fige"],"Perte de capteur devenue vert ou incident efface.")
        controls["qualite_et_alerte_memoire_conservees"] = True

        samples = [{"horodatage":"2026-10-05T02:00:00+00:00","conduite":"P2","debit_l_s":1.,"pression_m":38.},
                   {"horodatage":"2026-10-12T02:00:00+00:00","conduite":"P2","debit_l_s":2.,"pression_m":36.}]
        weekly = weekly_profile(samples)
        check(len(weekly["classes"]) == 1 and weekly["classes"][0]["jours_distincts"] == 2 and weekly["classes"][0]["debit_l_s"]["centre"] == 1.5,"Reference hebdomadaire incorrecte.")
        refuses(lambda:weekly_profile([dict(samples[0],horodatage="2026-10-05T02:00:00")]),"Fuseau absent accepte.")
        controls["hebdomadaire_uniquement_references_datees_importees"] = True

        from aqua_v3_acoustique import correlate
        import numpy as np
        rng = np.random.default_rng(812)
        signal = rng.normal(size=1000)
        acoustic = {"conduite":"P2","frequence_hz":1000.,"distance_capteurs_m":120.,"vitesse_onde_m_s":1000.,
                    "synchronisation_confirmee":True,"amont":np.r_[np.zeros(10),signal,np.zeros(60)].tolist(),
                    "aval":np.r_[np.zeros(50),signal,np.zeros(20)].tolist()}
        located = correlate(acoustic)
        check(abs(located["position_depuis_amont_m"]-40) < 1e-10,"Signe du retard acoustique incorrect.")
        refuses(lambda:correlate(dict(acoustic,synchronisation_confirmee=False)),"Horloges non synchronisees acceptees.")
        constant = correlate(dict(acoustic,amont=[1.]*1070,aval=[1.]*1070))
        check(constant["position_depuis_amont_m"] is None,"Position acoustique inventee sans energie.")
        sinus = np.sin(np.arange(1070)*.5).tolist()
        ambiguous = correlate(dict(acoustic,amont=sinus,aval=sinus))
        check(ambiguous["position_depuis_amont_m"] is None,"Pics acoustiques concurrents non rejetes.")
        controls["acoustique_retard_signe_position_connue_rejet_signaux_insuffisants_ambigus"] = True

        print("PLATEFORME V3 : contraintes, contexte, approbation et retours de vannes...",flush=True)
        normal = default_context()
        check(evaluate(normal,"reduire_A")["autorisee"],"Action nominale refusee.")
        for name, changes in [("capteur",{"qualite_capteurs":False}),("communication",{"communication":False}),
                              ("manuel",{"mode":"manuel"}),("perime",{"recu_epoch_s":time.time()-200}),
                              ("pression_basse",{"charge_source_m":15.}),("pression_haute",{"charge_source_m":90.})]:
            refused = evaluate(dict(normal,**changes),"reduire_A")
            check(not refused["autorisee"],"Contrainte ignoree : "+name)
        check(not evaluate(normal,"isoler_B")["autorisee"],"Coupure critique acceptee sans autorisation.")
        allow = evaluate(dict(normal,usagers_B=420),"isoler_B",True,"Essai d'isolement critique autorise.")
        check(allow["autorisee"] and allow["usagers_zones_privees_service"] == 420,"Isolement critique autorise mal traite.")
        check(not evaluate(dict(normal,mode="automatique"),"isoler_A")["autorisee"],"Isolement automatique accepte.")
        for action in ["reduire_A","reduire_B","equilibrer"]:
            result = evaluate(normal,action)
            previous = normal["ouvertures_pct"]
            for step in result["etapes"]:
                check(max(abs(previous[z]-step["ouvertures_pct"][z]) for z in "AB") <= 10.00001,"Variation de vanne depassee.")
                previous = step["ouvertures_pct"]
        controls["securite_qualite_communication_age_modes_pressions_vitesse_zone_critique"] = True
        ops = Operations(tmp/"operations.db")
        p = ops.propose("reduire_A")
        refuses(lambda:ops.execute(p["id"],False),"Action sans accord operateur.")
        with ThreadPoolExecutor(max_workers=3) as pool:
            executions = list(pool.map(lambda _:ops.execute(p["id"],True),range(3)))
        check(sum(not x["requete_rejouee"] for x in executions) == 1,"Action executee plusieurs fois.")
        check(ops.get("contexte")["phase"] == "attente_verification","Controle repris avant retour.")
        verified = ops.verify(p["id"])
        check(verified["verification"]["conforme"] and ops.get("contexte")["phase"] == "pret","Retour nominal non verifie.")
        for fault in ["vanne_bloquee","capteur_absent","reponse_lente"]:
            ctx = ops.get("contexte")
            ops.set_context({"qualite_capteurs":True,"communication":True},ctx["version"])
            p = ops.propose("reduire_B")
            ops.execute(p["id"],True)
            verified = ops.verify(p["id"],fault)
            check(not verified["verification"]["conforme"] and ops.get("contexte")["phase"] == "gel_ecart_observe","Defaut non bloquant : "+fault)
        ctx = ops.get("contexte")
        ops.set_context({"qualite_capteurs":True,"communication":True},ctx["version"])
        p = ops.propose("reduire_A")
        ctx = ops.get("contexte")
        ops.set_context({"demande_A_l_s":5.},ctx["version"])
        refuses(lambda:ops.execute(p["id"],True),"Proposition valide apres modification du contexte.")
        check(Operations(tmp/"operations.db").get("contexte")["version"] == ops.get("contexte")["version"],"Contexte perdu au redemarrage.")
        controls["approbation_concurrence_idempotence_contexte_persistant_et_retours_defaillants"] = True

        print("PLATEFORME V3 : interventions, assistant, rapports et API...",flush=True)
        from aqua_api_v3 import creer_app
        with TestClient(creer_app(base,root,tmp/"http_v3.db",tmp/"http_v2.db",worker=False)) as client:
            for route in ["/","/v2","/v1","/assets/aqua_v3.js","/assets/aqua_v2.js","/api/v1/sante","/api/v2/sante","/api/v3/sante","/openapi.json"]:
                check(client.get(route).status_code == 200,"Route absente : "+route)
            s = client.post('/api/v2/sessions',json={"origine":"simulation","source":"api","scenario_id":"controle"}).json()
            hs = s["id"]
            def ingest(i):
                r = client.post(f'/api/v2/sessions/{hs}/mesures',json={"origine":"simulation","sequence":i,"mesure":measure(i,reference,calibration)})
                check(r.status_code == 200,"Mesure HTTP refusee : "+r.text)
            for i in range(10):
                ingest(i)
            before = client.get(f'/api/v3/sessions/{hs}/analyse').json()
            r = client.post(f'/api/v3/sessions/{hs}/complements',json={"origine":"simulation","sequence":11})
            check(r.status_code == 422,"Complement futur accepte.")
            r = client.post(f'/api/v3/sessions/{hs}/complements',json={"origine":"terrain","sequence":9})
            check(r.status_code == 422,"Terrain accepte comme simulation.")
            good = {"origine":"simulation","sequence":9,"acoustique_db":{"P2":65},"reference_acoustique_db":{"P2":45}}
            check(client.post(f'/api/v3/sessions/{hs}/complements',json=good).status_code == 200,"Complement refuse.")
            check(client.post(f'/api/v3/sessions/{hs}/complements',json=good).status_code == 200,"Doublon de complement refuse.")
            check(client.post(f'/api/v3/sessions/{hs}/complements',json=dict(good,acoustique_db={"P2":70})).status_code == 422,"Complement modifie retroactivement.")
            check(client.post('/api/v3/baselines/hebdomadaire',json={"origine":"simulation","references":samples}).status_code == 200,"Reference hebdomadaire HTTP refusee.")
            ac = client.post('/api/v3/acoustique/localiser',json={**acoustic,"origine":"simulation","session_id":hs,"sequence":9})
            check(ac.status_code == 200 and ac.json()["position_depuis_amont_m"] == 40.,"Experience acoustique HTTP en echec.")
            check(client.get(f'/api/v3/sessions/{hs}/mesures/9').json()["sortie"]["sequence"] == 9,"Preuve de mesure non precise.")
            # Intervention sur un secteur sain : la cloture requiert quand
            # meme trois nouveaux points apres demande de verification.
            body = {"origine":"simulation","session_id":hs,"conduite":"P2","responsable":"<script>TEST</script>",
                    "description":"Inspection de demonstration & controle", "cle":"verification-intervention"}
            order = client.post('/api/v3/interventions',json=body).json()
            check(client.post('/api/v3/interventions',json=body).json()["id"] == order["id"],"Intervention dupliquee.")
            check(client.post('/api/v3/interventions',json=dict(body,description="Autre contenu")).status_code == 422,"Conflit de cle accepte.")
            path = '/api/v3/interventions/'+order["id"]
            check(client.patch(path,json={"version":0,"statut":"cloturee","note":"Non execute"}).status_code == 422,"Transition interdite acceptee.")
            order = client.patch(path,json={"version":0,"statut":"en_cours","note":"Inspection commencee"}).json()
            order = client.patch(path,json={"version":1,"statut":"a_verifier","note":"Recevoir un nouveau controle"}).json()
            check(client.patch(path,json={"version":2,"statut":"cloturee","note":"Controle fait"}).status_code == 422,"Cloture sans mesures nouvelles acceptee.")
            for i in range(10,13):
                ingest(i)
            closed = client.patch(path,json={"version":2,"statut":"cloturee","note":"Trois nouvelles mesures conformes"})
            check(closed.status_code == 200 and "preuve_apres" in closed.json(),"Cloture avec preuve refusee.")
            controls["interventions_transitions_preuves_avant_apres_trois_mesures_nouvelles"] = True
            with patch.dict(os.environ,{"AQUA_GROQ_API_KEY":"","AQUA_GROQ_MODEL":""}):
                proposals_before = len(client.get('/api/v3/controle/propositions').json()["propositions"])
                for question in ["Quelles fuites actives ?","Pourquoi P2 est critique ?","Quels capteurs sont en panne ?", "Quels risques de rupture ?","Quelle zone perd le plus ?","Que se passe-t-il si je ferme V12 ?","Que se passe-t-il si je ferme VA ?"]:
                    a = client.post('/api/v3/assistant',json={"session_id":hs,"question":question})
                    check(a.status_code == 200 and not a.json()["commande_executee"] and all(f["source"] for f in a.json()["faits"]),"Reponse non sourcee ou action executee.")
                    if "V12" in question:
                        check(a.json()["suggestion_sans_execution"] is None,"Vanne inexistante remappee.")
                    if "VA" in question:
                        check(a.json()["simulation_sans_execution"] is not None,"Question d'isolement sans simulation.")
                        saved = client.get('/api/v3/assistant/reponses/'+a.json()["reponse_id"])
                        check(saved.status_code == 200 and saved.json()["reponse"] == a.json()["reponse"],"Preuve de reponse non conservable.")
                check(len(client.get('/api/v3/controle/propositions').json()["propositions"]) == proposals_before,"L'assistant a cree une proposition de commande.")
            snap = client.get(f'/api/v3/sessions/{hs}/analyse').json()
            # Le LLM est remplace par une reponse de transport controlee. Il
            # ne s'agit pas d'un essai du service distant ou d'une cle reelle.
            priorities = enregistrements(base.priorites)
            def fake(ids):
                return io.BytesIO(json.dumps({"choices":[{"message":{"content":json.dumps({"ids":ids})}}]}).encode())
            with patch.dict(os.environ,{"AQUA_GROQ_API_KEY":"cle_fictive_non_utilisee","AQUA_GROQ_MODEL":"modele_fictif"}):
                with patch('aqua_v3_agent.urlopen',return_value=fake(["etat_P2"])):
                    a = answer("Resumer P2",snap,[],priorities,normal)
                    check(a["mode"].startswith("LLM") and all(f["id"] in {"etat_P2","limites"} for f in a["faits"]),"Selection LLM valide non acceptee.")
                with patch('aqua_v3_agent.urlopen',return_value=fake(["fausse_probabilite_99"])):
                    a = answer("Resumer P2",snap,[],priorities,normal)
                    check(a["avertissement"] and not a["mode"].startswith("LLM"),"Identifiant invente par LLM accepte.")
            controls["assistant_source_local_noms_inconnus_llm_simule_avec_rejet_inventions"] = True
            for format in ["json","csv","html"]:
                r = client.get(f'/api/v3/sessions/{hs}/rapport?format='+format)
                check(r.status_code == 200,"Rapport indisponible.")
                if format == "html":
                    check('<script>TEST</script>' not in r.text and '&lt;script&gt;TEST&lt;/script&gt;' in r.text,"Rapport interprete du HTML operateur.")
                if format == "json":
                    check(r.json()["sha256_snapshot"] == digest(r.json()["analyse"]),"Empreinte du rapport incoherente.")
            controls["rapports_JSON_CSV_HTML_imprimable_et_echappement"] = True
            # Un cycle automatique reste un calcul explicite du laboratoire.
            context = client.get('/api/v3/controle/contexte').json()
            check(client.put('/api/v3/controle/contexte',json={"origine":"simulation","version":context["version"],"mode":"automatique"}).status_code == 200,"Mode auto indisponible.")
            auto = client.post('/api/v3/controle/cycle_automatique',json={"origine":"simulation","approuver":True})
            check(auto.status_code == 200 and auto.json()["etat"] == "verifiee_simulation","Cycle automatique non verifie.")
            audit = client.get('/api/v3/audit').json()
            check(audit["integrite_chaine"] and len(audit["evenements"])>5,"Journal incoherent.")
        with TestClient(creer_app(base,root,tmp/"http_v3.db",tmp/"http_v2.db",worker=False)) as client:
            check(client.get('/api/v3/interventions').json()["interventions"][0]["statut"] == "cloturee","Intervention perdue apres redemarrage.")
        controls["API_regression_V1_V2_redemarrage_cycle_auto_audit"] = True
        with ops.db() as c:
            c.execute("UPDATE audit SET contenu='{}' WHERE numero=1")
        check(not ops.audit()["integrite_chaine"],"Alteration du journal non detectee.")
        controls["audit_chaines_alteration_detectee"] = True

    check(all(Path(p).is_file() and sha256(Path(p)) == h for p,h in frozen.items()),"Fichier historique modifie.")
    controls["fichiers_figes_inchanges"] = len(frozen)
    report.update(verification="PASS",duree_s=round(time.monotonic()-started,2),
                  limite="Tests techniques sur donnees et retours artificiels. Aucun nouveau score de generalisation, aucune surete industrielle ni validation terrain. LLM distant non appele ; contrat teste par doublure.")
    sources = ["aqua_api_v3.py","aqua_v3_monitor.py","aqua_v3_ops.py","aqua_v3_agent.py","aqua_v3_acoustique.py","aqua_interface_v3.html","aqua_interface_v3.js","aqua_verifier_plateforme_v3.py"]
    report["sources_sha256"] = {n:sha256(root/n) for n in sources}
    destination = root/"resultats_aqua/plateforme_v3"/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'_'+uuid4().hex[:6])
    destination.mkdir(parents=True)
    (destination/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print("PLATEFORME V3 : PASS technique - "+str(len(controls))+" groupes de controles.")
    print("Aucun equipement physique commande. Resultats : "+str(destination))
    return report


if __name__ == "__main__":
    verifier()
