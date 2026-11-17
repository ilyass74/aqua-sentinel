r"""Producteur HTTP de mesures SIMULEES pour AQUA Sentinel V2.

  .venv\Scripts\python.exe aqua_producteur_v2.py --nombre 60 --intervalle .5

Lire les mesures V3 deja sauvegardees, creer une session source 'api', puis
envoyer une seule mesure par requete avec une sequence idempotente. Le
serveur doit etre lance. Aucun fichier de verite n'est lu, aucun capteur
physique n'est raccorde. Ce processus peut etre arrete avec Ctrl+C.
"""
import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aqua_api_v1 import EtatAqua, enregistrements


def post(url, payload):
    data = json.dumps(payload, allow_nan=False).encode()
    request = Request(url, data=data, headers={"Content-Type":"application/json"}, method="POST")
    for attempt in range(3):
        try:
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"API refusee ({exc.code}) : " + exc.read().decode("utf-8","replace")) from None
        except (URLError,TimeoutError):
            if attempt==2:
                raise
            time.sleep(.25)


def main():
    p=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port",type=int,default=8013)
    p.add_argument("--nombre",type=int,default=60)
    p.add_argument("--intervalle",type=float,default=.5)
    p.add_argument("--scenario")
    p.add_argument("--debut-index",type=int,default=0)
    a=p.parse_args()
    if not 1024<=a.port<=65535 or not 1<=a.nombre<=1440 or not .01<=a.intervalle<=10 or not 0<=a.debut_index<=1439:
        p.error("Port, nombre, intervalle ou index hors limites de demonstration.")
    state=EtatAqua()
    scenario=a.scenario or state.defaut["scenario_id"]
    if scenario not in state.groupes_capteurs:
        p.error("Scenario inconnu.")
    source=state.groupes_capteurs[scenario].iloc[a.debut_index:a.debut_index+a.nombre]
    root=f"http://127.0.0.1:{a.port}"
    session=post(root+"/api/v2/sessions",{"origine":"simulation","source":"api","scenario_id":scenario})
    print("Session API : "+session["id"],flush=True)
    try:
        for seq,measure in enumerate(enregistrements(source)):
            result=post(root+"/api/v2/sessions/"+session["id"]+"/mesures",{"origine":"simulation","sequence":seq,"mesure":measure})
            print(f"[{seq+1}/{len(source)}] t={measure['temps_s']} s : {result['etat_global']}",flush=True)
            if seq+1<len(source):
                time.sleep(a.intervalle)
    except KeyboardInterrupt:
        print("\nProducteur arrete ; les mesures deja acceptees restent conservees.")


if __name__=="__main__":
    main()
