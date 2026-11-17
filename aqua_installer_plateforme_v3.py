"""Installe la couche V3 et ajoute V2 uniquement si ses fichiers sont absents.

Lancer le fichier depuis le dossier extrait du ZIP avec le Python .venv du
projet : python aqua_installer_plateforme_v3.py --projet CHEMIN_DU_PROJET
Les donnees, bases et modeles ne sont jamais contenus dans la livraison.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone
from uuid import uuid4

V2 = {"aqua_api_v2.py","aqua_v2_core.py","aqua_v2_hydraulique.py","aqua_interface_v2.html",
      "aqua_interface_v2.js","aqua_verifier_v2.py","aqua_producteur_v2.py","requirements_api_v2.txt"}
V3 = {"aqua_api_v3.py","aqua_v3_monitor.py","aqua_v3_ops.py","aqua_v3_agent.py","aqua_v3_acoustique.py",
      "aqua_interface_v3.html","aqua_interface_v3.js","aqua_verifier_plateforme_v3.py","requirements_api_v3.txt",
      "aqua_installer_plateforme_v3.py","AQUA_V3_GUIDE.md","AQUA_V3_COMPARAISON.md","AQUA_V3_VERIFICATION.json",
      "AQUA_V3_EXEMPLE_HEBDOMADAIRE.json","AQUA_V3_EXEMPLE_ACOUSTIQUE.json"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(package, project):
    package, project = Path(package).resolve(),Path(project).resolve()
    manifest = json.loads((package/"AQUA_V3_MANIFESTE.json").read_text(encoding="utf-8"))
    expected = manifest["fichiers"]
    needed = V3|{"compat_v2/"+n for n in V2}
    if set(expected) != needed:
        raise ValueError("La liste de fichiers de cette livraison ne correspond pas a l'installateur.")
    required = ["aqua_api_v1.py","aqua_interface_v1.html","requirements_api_v1.txt","aqua_detection_v3.py","aqua_risque_v1.py"]
    absent = [n for n in required if not (project/n).is_file()]
    if absent:
        raise ValueError("Projet V1 incomplet ou mauvais dossier : "+", ".join(absent))
    plan = []
    for relative, fingerprint in expected.items():
        source = package/relative
        if not source.is_file() or sha(source) != fingerprint:
            raise ValueError("Fichier absent ou altere dans le ZIP : "+relative)
        target = project/source.name
        if relative.startswith("compat_v2/") and target.exists():
            if sha(target) != fingerprint:
                raise ValueError("Fichier V2 local different de la livraison connue : "+target.name+". Il est conserve ; installation arretee avant toute copie.")
            continue
        if target.is_file() and sha(target) == fingerprint:
            continue
        plan.append((source,target))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid4().hex[:6]
    backup = project/"resultats_aqua/sauvegardes_installation_v3"/stamp
    backup.mkdir(parents=True)
    created, replaced = [], []
    try:
        for source,target in plan:
            if target.exists():
                if not target.is_file():
                    raise ValueError("Destination non reguliere : "+target.name)
                shutil.copy2(target,backup/target.name)
                replaced.append(target)
            else:
                created.append(target)
            stage = target.with_name(target.name+'.aqua_v3_nouveau_'+stamp)
            try:
                shutil.copy2(source,stage)
                os.replace(stage,target)
            finally:
                stage.unlink(missing_ok=True)
        check = {target.name:sha(target) for _,target in plan}
        if any(check[target.name] != sha(source) for source,target in plan):
            raise ValueError("Controle des copies en echec.")
    except Exception:
        for target in reversed(replaced):
            shutil.copy2(backup/target.name,target)
        for target in created:
            target.unlink(missing_ok=True)
        raise
    receipt = {"installation":"PASS","projet":str(project),"fichiers_copies":len(plan),"sauvegardes":str(backup),
               "modeles_donnees_et_bases_modifies":False,"empreintes_copies":check}
    (backup/"installation.json").write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Installation V3 : PASS. "+str(len(plan))+" fichier(s) ajoute(s) ou mis a jour.")
    print("Les sources V1, modeles, donnees et bases existantes ont ete conserves.")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projet",type=Path,required=True)
    args = parser.parse_args()
    try:
        install(Path(__file__).resolve().parent,args.projet)
    except (OSError,ValueError,KeyError) as exc:
        print("Installation V3 interrompue : "+str(exc),file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
