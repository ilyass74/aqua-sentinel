r"""AQUA Sentinel : risque futur / priorite d'inspection, PROTOTYPE SYNTHETIQUE.

Lire ce code avant integration. Memes dependances que le ML existant.
  .venv\Scripts\python.exe aqua_risque_v1.py entrainer
  .venv\Scripts\python.exe aqua_risque_v1.py evaluer
  .venv\Scripts\python.exe aqua_risque_v1.py illustrer

Objectif experimental : classer des actifs EN SERVICE a une date t, selon
la survenue d'un NOUVEL incident pendant ]t, t+30 jours]. Le choix de
30 jours est un horizon de demonstration, pas une capacite deja prouvee.
Il ne s'agit ni de detecter une fuite actuelle ni de dater une rupture.

Nous n'avons aucun historique reel d'age, de materiau, d'inspection ou de
casse. Un generateur ARTIFICIEL cree donc des conduites et des raccords,
leurs observations passees et des incidents futurs aleatoires. M1/M2/M3
sont des categories fictives, SANS correspondance avec des materiaux reels.
La loi d'incident ci-dessous et tous ses coefficients sont inventes pour
tester le logiciel : ce ne sont PAS des lois de resistance des materiaux.
Le generateur n'utilise pas WNTR et n'ajoute pas de mesures reelles a V3.

Protocole fixe : 6 000 actifs train observes le 2023-10-01 ; 2 000 autres
actifs validation le 2024-10-01 ; 2 000 autres actifs test le 2025-10-01.
Chaque actif a UNE observation et 30 jours de mesures fictives passees.
Les trois cohortes sont distinctes ; ce n'est pas un suivi longitudinal
d'un meme reseau. Suivi futur complet de 30 jours, aucune censure simulee.
Tous les actifs fonctionnent a t ; les incidents sont tires APRES t.

Les variables utilisent seulement l'inventaire, une inspection passee et
les 30 jours termines a t. Les etiquettes et dates futures sont separees.
Pretraitements appris sur train uniquement. Deux candidats FIXES :
regression logistique avec age seul, ou avec l'ensemble des attributs.
Choix sur validation par average precision, puis age seul en cas d'egalite.
Aucun reglage sur test. Une reference constante utilise le taux de train.

Le modele, le code et le protocole sont figes AVANT generation du test.
Les predictions des trois methodes sont sauvees AVANT lecture des labels
par l'evaluateur. Un verrou interdit de reutiliser ce test pour un autre gel.
Le generateur peut bien entendu creer/auditer la verite ; il ne l'expose
jamais a la fonction de prediction.

Verification locale : un premier test de developpement (graine 20261020)
a revele un trace inadequat de la reference constante. Correction du
graphe en escaliers, sans changer les variables, modeles, hyperparametres
ou regle de selection. Le test FINAL utilise la graine 20261021, fixee
avant sa generation. L'ancien test n'est plus presente comme inedit.

Sorties : rang, indice relatif 0..100 (percentile parmi les scores de
train), contributions additives du modele et proposition d'inspection
pour les 20 % premiers de chaque lot/date. Ce quota est une hypothese de
capacite de demonstration, PAS un seuil physique de danger. Un indice
de 90 ne signifie PAS 90 % de risque. Le score interne 0..1 n'est pas
une probabilite calibree pour un vrai reseau.

illustrer cree 5 conduites P1..P5 et 4 raccords fictifs J_A..J_D associes
aux noeuds A..D de la maquette. Seules les dimensions des 5 conduites
reprennent la geometrie connue. Ages, categories, etats et historiques
sont TOUS inventes ; les noeuds hydrauliques ne prouvent pas l'existence
d'un raccord physique documente. Rien n'est inferre sur leur etat reel.

Limites : un bon score mesure l'apprentissage d'une loi artificielle,
pas la prediction de casses reelles. Pas d'effet du sol, de corrosion
physique, de politique de reparation, de derive temporelle ou de capteurs
defaillants valide. Avant un usage reel : inventaire documente, incidents
et non-incidents dates, suivi des actifs, controles de censure, evaluation
chronologique et calibration sur un reseau reel. L'entree de prediction
de ce prototype est volontairement reservee a l'origine synthetique.

Sources de methode (elles ne fournissent PAS nos donnees/coefficients) :
https://arxiv.org/abs/2007.03744
https://scikit-learn.org/stable/common_pitfalls.html#data-leakage
https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html
https://scikit-learn.org/stable/modules/generated/sklearn.metrics.average_precision_score.html
"""

import argparse
import hashlib
import json
import math
import platform
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import joblib
import numpy as np
import pandas as pd
import sklearn
from scipy.special import expit
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from threadpoolctl import threadpool_limits


DOSSIER = Path(__file__).resolve().parent
HORIZON = 30
QUOTA = 0.20
GRAINE_TEST = 20261021
ORIGINE = "simulation_actifs"
CLES = ["actif_id", "date_observation"]
NUM = ["age_ans", "longueur_m", "diametre_mm", "reparations_passees",
       "etat_inspection", "jours_depuis_inspection", "pression_moyenne_30j_m",
       "amplitude_p95_30j_m", "jours_transitoires_30j"]
CAT = ["type_actif", "classe_materiau"]
META = ["origine", "date_fin_historique", "date_inspection"]
ENTREES = CLES + META + NUM + CAT
CANDIDATS = ["age_seul", "logistique_actifs"]
METHODES = ["constante_train"] + CANDIDATS
PARTIES = {"train": (6000, "2023-10-01", 1),
           "validation": (2000, "2024-10-01", 2),
           "test": (2000, "2025-10-01", 3),
           "demo": (9, "2026-10-16", 4)}
LIBELLES = {"age_ans": "age", "longueur_m": "longueur", "diametre_mm": "diametre",
            "reparations_passees": "reparations passees", "etat_inspection": "etat a l'inspection",
            "jours_depuis_inspection": "anciennete de l'inspection",
            "pression_moyenne_30j_m": "pression moyenne passee",
            "amplitude_p95_30j_m": "amplitude passee",
            "jours_transitoires_30j": "jours avec transitoire fictif"}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ecrire_json(path, contenu):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporaire = path.with_suffix(path.suffix + ".tmp")
    temporaire.write_text(json.dumps(contenu, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporaire.replace(path)


def ecrire_csv(path, frame, compresser=False):
    path = Path(path)
    temporaire = path.with_suffix(path.suffix + ".tmp")
    compression = {"method": "gzip", "mtime": 0} if compresser else None
    frame.to_csv(temporaire, index=False, float_format="%.15g", compression=compression)
    temporaire.replace(path)


def protocole():
    return {"version": 1, "origine": ORIGINE, "horizon_jours": HORIZON,
            "quota_inspection": QUOTA, "graine_train_validation": 20261020, "graine_test_final": GRAINE_TEST,
            "cohortes": {k: list(v) for k, v in PARTIES.items()},
            "variables_numeriques": NUM, "variables_categorielles": CAT,
            "candidats": CANDIDATS, "regle_choix": "AP validation maximale ; age seul si egalite",
            "regression": {"C": 1.0, "max_iter": 1000, "solver": "lbfgs", "class_weight": None},
            "validation_terrain": False, "loi_incident": "artificielle, coefficients dans le code",
            "cible": "nouvel incident dans ]t,t+30j], tous les actifs en service a t"}


def identite():
    return {"sha256_code": sha256(__file__), "protocole": protocole()}


def variables_passees(inventaire, historique):
    """Agrege 30 jours connus a t. Un enregistrement futur provoque une erreur."""
    inv = inventaire.copy().reset_index(drop=True)
    if inv.actif_id.duplicated().any() or historique.duplicated(["actif_id", "date_mesure"]).any():
        raise ValueError("Identifiant ou jour duplique.")
    if set(inv.actif_id) != set(historique.actif_id):
        raise ValueError("Actifs differents entre inventaire et historique.")
    h = historique.merge(inv[CLES], on="actif_id", validate="many_to_one")
    delta = (pd.to_datetime(h.date_observation) - pd.to_datetime(h.date_mesure)).dt.days
    if not delta.between(0, 29).all() or not h.groupby("actif_id").size().eq(30).all():
        raise ValueError("Historique incomplet ou hors des 30 jours passes ; aucune mesure future admise.")
    champs = ["pression_moyenne_j_m", "amplitude_j_m", "transitoire_j"]
    if not np.isfinite(h[champs].to_numpy(dtype=float)).all() or not h.transitoire_j.isin([0, 1]).all():
        raise ValueError("Historique numerique invalide.")
    agg = h.groupby("actif_id", sort=False).agg(
        pression_moyenne_30j_m=("pression_moyenne_j_m", "mean"),
        amplitude_p95_30j_m=("amplitude_j_m", lambda x: x.quantile(0.95)),
        jours_transitoires_30j=("transitoire_j", "sum"),
        date_fin_historique=("date_mesure", "max"))
    inv = inv.merge(agg, on="actif_id", validate="one_to_one")
    inv["jours_depuis_inspection"] = (pd.to_datetime(inv.date_observation) - pd.to_datetime(inv.date_inspection)).dt.days
    return verifier_entrees(inv[ENTREES])


def verifier_entrees(frame):
    if set(frame.columns) != set(ENTREES) or frame.columns.duplicated().any():
        raise ValueError("Schema invalide : seules les observations passees sont admises, aucune verite future.")
    x = frame.copy().reset_index(drop=True)
    if x.empty or x.isna().any().any() or x.duplicated(CLES).any() or x.actif_id.astype(str).str.len().eq(0).any():
        raise ValueError("Observations vides, manquantes ou dupliquees.")
    if not x.origine.eq(ORIGINE).all():
        raise ValueError("Prototype reserve a des actifs synthetiques ; aucune prediction reelle autorisee par ce modele.")
    if not x.type_actif.isin(["conduite", "raccord"]).all() or not x.classe_materiau.isin(["M1", "M2", "M3"]).all():
        raise ValueError("Type ou categorie fictive inconnue.")
    for c in ["date_observation", "date_fin_historique", "date_inspection"]:
        pd.to_datetime(x[c], format="%Y-%m-%d", errors="raise")
    t = pd.to_datetime(x.date_observation)
    delai = (t - pd.to_datetime(x.date_inspection)).dt.days
    if not x.date_fin_historique.eq(x.date_observation).all() or (delai < 0).any() or not delai.eq(x.jours_depuis_inspection).all():
        raise ValueError("Inspection future, historique desynchronise ou anciennete incoherente.")
    x[NUM] = x[NUM].astype(float)
    if not np.isfinite(x[NUM].to_numpy()).all() or (x[NUM] < 0).any().any():
        raise ValueError("Variable negative ou non finie.")
    if not x.etat_inspection.between(0, 100).all() or not x.jours_transitoires_30j.between(0, 30).all():
        raise ValueError("Etat fictif ou nombre de jours invalide.")
    for c in ["reparations_passees", "jours_transitoires_30j", "jours_depuis_inspection"]:
        if not np.equal(x[c], np.floor(x[c])).all():
            raise ValueError("Compte non entier : " + c)
    if (x.age_ans <= 0).any() or (x.diametre_mm <= 0).any():
        raise ValueError("Age ou diametre invalide.")
    if (x.jours_depuis_inspection > x.age_ans * 365).any():
        raise ValueError("Inspection anterieure a l'installation fictive.")
    if ((x.type_actif.eq("conduite") & x.longueur_m.le(0)) | (x.type_actif.eq("raccord") & x.longueur_m.ne(0))).any():
        raise ValueError("Longueur incoherente avec le type d'actif fictif.")
    return x[ENTREES]


def creer_cohorte(partie, decalage_graine_future=0):
    """Donnees inventees. Le flux aleatoire futur n'affecte aucune observation."""
    n, date, numero = PARTIES[partie]
    graine = GRAINE_TEST if partie == "test" else 20261020
    s = np.random.SeedSequence([graine, numero]).spawn(3)
    r, rh = np.random.default_rng(s[0]), np.random.default_rng(s[1])
    rf = np.random.default_rng(s[2] if decalage_graine_future == 0 else [graine, numero, decalage_graine_future])
    types = r.choice(["conduite", "raccord"], n, p=[0.7, 0.3])
    ids = [f"{partie}_{i:05d}" for i in range(n)]
    longueur = np.where(types == "conduite", r.uniform(40, 500, n), 0)
    diametre = r.choice([75, 100, 150, 200, 250, 300], n)
    if partie == "demo":
        ids = ["P1", "P2", "P3", "P4", "P5", "J_A", "J_B", "J_C", "J_D"]
        types = np.array(["conduite"] * 5 + ["raccord"] * 4)
        longueur = np.array([200, 120, 120, 80, 80, 0, 0, 0, 0])
        diametre = np.array([150, 100, 100, 100, 100, 100, 100, 100, 100])
    age = r.uniform(1, 65, n)
    ecart_inspection = r.integers(0, 731, n)
    # Meme une inspection synthetique doit survenir apres l'installation.
    ecart_inspection = np.minimum(ecart_inspection, np.floor(age * 365).astype(int))
    inspection = pd.Timestamp(date) - pd.to_timedelta(ecart_inspection, unit="D")
    inv = pd.DataFrame({"actif_id": ids, "date_observation": date, "origine": ORIGINE,
        "date_inspection": inspection.strftime("%Y-%m-%d"), "type_actif": types,
        "classe_materiau": r.choice(["M1", "M2", "M3"], n, p=[0.5, 0.3, 0.2]),
        "age_ans": age, "longueur_m": longueur, "diametre_mm": diametre,
        "reparations_passees": r.poisson(age / 65),
        "etat_inspection": np.clip(0.7 * (age - ecart_inspection / 365) + r.normal(8, 16, n), 0, 100)})
    jours = pd.date_range(end=date, periods=30).strftime("%Y-%m-%d").to_numpy()
    niveau = rh.uniform(28, 60, (n, 1))
    amplitude = rh.uniform(1, 9, (n, 1))
    transitoires = rh.binomial(1, rh.beta(1.7, 7, (n, 1)), (n, 30))
    historique = pd.DataFrame({"actif_id": np.repeat(ids, 30), "date_mesure": np.tile(jours, n),
        "pression_moyenne_j_m": (niveau + rh.normal(0, 0.5, (n, 30))).ravel(),
        "amplitude_j_m": (amplitude + rh.gamma(2, 0.5, (n, 30))).ravel(),
        "transitoire_j": transitoires.ravel()})
    x = variables_passees(inv, historique)
    if partie == "demo":
        return x, historique, None
    # LOI ARTIFICIELLE de demonstration. Aucun de ces coefficients n'est
    # estime sur un service d'eau. Le choc futur demeure inconnu du modele.
    z = (-6.0 + 0.03 * x.age_ans + 0.035 * x.etat_inspection
         + 0.18 * x.reparations_passees + 0.07 * x.jours_transitoires_30j
         + 0.06 * (x.pression_moyenne_30j_m - 40)
         + 0.08 * (x.amplitude_p95_30j_m - 4)
         + 0.0015 * np.where(x.type_actif.eq("conduite"), x.longueur_m - 120, 0)
         + x.classe_materiau.map({"M1": 0, "M2": 0.45, "M3": -0.35})
         + 0.30 * x.type_actif.eq("raccord") + rf.normal(0, 0.8, n))
    y = rf.binomial(1, expit(z))
    futurs = (pd.Timestamp(date) + pd.to_timedelta(rf.integers(1, HORIZON + 1, n), unit="D")).strftime("%Y-%m-%d")
    verite = x[CLES].copy()
    verite["defaillance_30j"] = y
    verite["date_defaillance"] = np.where(y == 1, futurs, "")
    verite["date_fin_suivi"] = (pd.Timestamp(date) + pd.Timedelta(days=HORIZON)).strftime("%Y-%m-%d")
    return x, historique, verite


def preparer_partie(destination, partie):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "_SUCCESS").exists():
        audit = json.loads((destination / "audit.json").read_text(encoding="utf-8"))
        for nom, empreinte in audit["fichiers_sha256"].items():
            if sha256(destination / nom) != empreinte:
                raise ValueError("Dataset modifie : " + nom)
        return
    print(f"Generation {partie} : {PARTIES[partie][0]} actifs fictifs...", flush=True)
    x, h, y = creer_cohorte(partie)
    if y is None:
        raise ValueError("La demonstration ne comporte aucune verite future.")
    ecrire_csv(destination / "observations.csv", x)
    ecrire_csv(destination / "historique_30j.csv.gz", h, True)
    ecrire_csv(destination / "verite.csv", y)
    ecrire_json(destination / "audit.json", {"origine": ORIGINE, "partie": partie, "actifs": len(x),
        "aucune_etiquette_dans_observations": True, "historique_jours_par_actif": 30,
        "suivi_complet_jours": HORIZON, "tous_actifs_en_service_a_t": True,
        "fichiers_sha256": {f: sha256(destination / f) for f in
            ["observations.csv", "historique_30j.csv.gz", "verite.csv"]}})
    (destination / "_SUCCESS").write_text("PASS technique, simulation uniquement\n", encoding="utf-8")


def lire_observations(dossier):
    return verifier_entrees(pd.read_csv(Path(dossier) / "observations.csv"))


def lire_verite(dossier, x):
    y = pd.read_csv(Path(dossier) / "verite.csv", keep_default_na=False)
    attendues = CLES + ["defaillance_30j", "date_defaillance", "date_fin_suivi"]
    if set(y.columns) != set(attendues) or y.duplicated(CLES).any() or len(y) != len(x):
        raise ValueError("Schema ou identifiants de verite invalides.")
    y = x[CLES].merge(y, on=CLES, validate="one_to_one")
    if y.isna().any().any() or not y.defaillance_30j.isin([0, 1]).all():
        raise ValueError("Etiquettes manquantes ou invalides.")
    t = pd.to_datetime(y.date_observation)
    fin = t + pd.Timedelta(days=HORIZON)
    if not pd.to_datetime(y.date_fin_suivi).eq(fin).all():
        raise ValueError("Suivi futur incomplet : la censure ne peut pas etre traitee comme un negatif.")
    positifs = y.defaillance_30j.eq(1)
    dates = pd.to_datetime(y.loc[positifs, "date_defaillance"])
    if not ((dates > t[positifs]) & (dates <= fin[positifs])).all() or y.loc[~positifs, "date_defaillance"].ne("").any():
        raise ValueError("Incident hors de la fenetre future.")
    return y


def nouveau_modele(nom):
    num = ["age_ans"] if nom == "age_seul" else NUM
    transformeurs = [("num", StandardScaler(), num)]
    if nom == "logistique_actifs":
        transformeurs.append(("cat", OneHotEncoder(drop="first", handle_unknown="error", sparse_output=False), CAT))
    return Pipeline([("variables", ColumnTransformer(transformeurs, remainder="drop")),
                     ("logistique", LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000))])


def classer(x, scores, reference):
    p = x[CLES + ["origine", "type_actif"]].copy()
    p["score_interne_non_calibre_terrain"] = np.asarray(scores)
    p["indice_relatif_0_100"] = 50 * (np.searchsorted(reference, scores, side="left")
        + np.searchsorted(reference, scores, side="right")) / len(reference)
    p["fin_horizon_illustratif"] = (pd.to_datetime(p.date_observation) + pd.Timedelta(days=HORIZON)).dt.strftime("%Y-%m-%d")
    p = p.sort_values(["date_observation", "score_interne_non_calibre_terrain", "actif_id"], ascending=[True, False, True])
    p["rang_dans_lot"] = p.groupby("date_observation", sort=False).cumcount() + 1
    capacite = p.groupby("date_observation").actif_id.transform("size").mul(QUOTA).apply(math.ceil)
    p["inspection_prioritaire"] = p.rang_dans_lot.le(capacite).astype(int)
    return p.reset_index(drop=True)


def predire(x, paquet, nom=None):
    """Observations uniquement. Ni verite, ni date d'incident, ni graine future."""
    x = verifier_entrees(x)
    nom = nom or paquet["retenu"]
    with threadpool_limits(limits=2):
        scores = (np.full(len(x), paquet["taux_train"]) if nom == "constante_train"
                  else paquet["modeles"][nom].predict_proba(x)[:, 1])
    p = classer(x, scores, paquet["references"][nom])
    hors_plage = ((x[NUM] < pd.Series(paquet["minimums_train"])) | (x[NUM] > pd.Series(paquet["maximums_train"]))).any(axis=1)
    p = p.merge(x[CLES].assign(hors_plage_train=hors_plage.astype(int)), on=CLES, validate="one_to_one")
    return p


def metriques(p, verite):
    z = p.merge(verite, on=CLES, validate="one_to_one")
    if len(z) != len(p):
        raise ValueError("Predictions et verite desalignees.")
    y, inspectes = z.defaillance_30j.to_numpy(), z.inspection_prioritaire.eq(1).to_numpy()
    positifs, k = int(y.sum()), int(inspectes.sum())
    trouves = int(y[inspectes].sum())
    prevalence = float(y.mean())
    return {"actifs": len(z), "incidents_futurs": positifs, "actifs_proposes_inspection": k,
        "incidents_parmi_inspectes": trouves, "inspections_sans_incident_futur": k - trouves,
        "taux_incidents": prevalence,
        "average_precision": float(average_precision_score(y, z.score_interne_non_calibre_terrain)) if positifs else None,
        "auc_roc": float(roc_auc_score(y, z.score_interne_non_calibre_terrain)) if 0 < positifs < len(z) else None,
        "precision_quota": trouves / k if k else 0,
        "rappel_quota": trouves / positifs if positifs else None,
        "lift_quota": (trouves / k) / prevalence if k and prevalence else None}


def afficher(titre, resultats, retenu):
    print("\n" + titre + " (DONNEES ARTIFICIELLES)")
    lignes = [{"methode": k, "AP": v["average_precision"], "inspections": v["actifs_proposes_inspection"],
               "incidents_cibles": f'{v["incidents_parmi_inspectes"]}/{v["incidents_futurs"]}',
               "precision_quota": v["precision_quota"], "rappel_quota": v["rappel_quota"],
               "lift": v["lift_quota"]} for k, v in resultats.items()]
    print(pd.DataFrame(lignes).round(3).to_string(index=False))
    print("Modele retenu sur validation : " + retenu)
    print("Quota : 20 % des actifs du lot ; pas un seuil physique ni une probabilite de rupture.")


def dernier_modele():
    racine = DOSSIER / "models" / "aqua_risque_v1"
    candidats = sorted([p for p in racine.glob("*") if (p / "_GELE").is_file()], reverse=True)
    if not candidats:
        raise ValueError("Lancer d'abord : aqua_risque_v1.py entrainer")
    return candidats[0]


def charger_modele(run):
    run = Path(run).resolve()
    if not (run / "_GELE").is_file():
        raise ValueError("Modele incomplet ou non fige.")
    meta = json.loads((run / "gel.json").read_text(encoding="utf-8"))
    if meta["identite"] != identite():
        raise ValueError("Code ou protocole modifie depuis le gel. Ne pas regler ce modele sur son test.")
    if meta["versions"]["sklearn"] != sklearn.__version__:
        raise ValueError("Version scikit-learn differente de celle du gel.")
    for fichier, empreinte in meta["artefacts_sha256"].items():
        if sha256(run / fichier) != empreinte:
            raise ValueError("Artefact fige modifie : " + fichier)
    return joblib.load(run / "modele.joblib"), meta


def entrainer():
    ident = identite()
    racine_modeles = DOSSIER / "models" / "aqua_risque_v1"
    for precedent in sorted(racine_modeles.glob("*"), reverse=True):
        if (precedent / "_GELE").exists() and json.loads((precedent / "gel.json").read_text(encoding="utf-8"))["identite"] == ident:
            _, meta = charger_modele(precedent)
            afficher("VALIDATION DEJA FIGEE", meta["validation"], meta["retenu"])
            print(f"Modele : {precedent}")
            return precedent
    signature = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:12]
    data = DOSSIER / "data" / "aqua_risque_v1" / signature
    data.mkdir(parents=True, exist_ok=True)
    ecrire_json(data / "protocole.json", protocole())
    for partie in ["train", "validation"]:
        preparer_partie(data / partie, partie)
    train, val = lire_observations(data / "train"), lire_observations(data / "validation")
    yt, yv = lire_verite(data / "train", train), lire_verite(data / "validation", val)
    if set(train.actif_id) & set(val.actif_id) or not (pd.to_datetime(yt.date_fin_suivi).max() < pd.to_datetime(val.date_observation).min()):
        raise ValueError("Cohortes communes ou fenetres temporelles qui se chevauchent.")
    paquet = {"modeles": {}, "references": {}, "retenu": "age_seul", "taux_train": float(yt.defaillance_30j.mean()),
              "minimums_train": train[NUM].min().to_dict(), "maximums_train": train[NUM].max().to_dict()}
    paquet["references"]["constante_train"] = np.full(len(train), paquet["taux_train"])
    for nom in CANDIDATS:
        print("Apprentissage train uniquement : " + nom, flush=True)
        m = nouveau_modele(nom)
        with threadpool_limits(limits=2):
            m.fit(train, yt.defaillance_30j)
            ref = m.predict_proba(train)[:, 1]
        if int(m.named_steps["logistique"].n_iter_.max()) >= 1000:
            raise ValueError("Convergence non atteinte.")
        paquet["modeles"][nom] = m
        paquet["references"][nom] = np.sort(ref)
    predictions = {nom: predire(val, paquet, nom) for nom in METHODES}
    scores = {nom: metriques(p, yv) for nom, p in predictions.items()}
    paquet["retenu"] = max(CANDIDATS, key=lambda n: (scores[n]["average_precision"], -CANDIDATS.index(n)))
    identifiant = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
    run = racine_modeles / identifiant
    run.mkdir(parents=True)
    joblib.dump(paquet, run / "modele.joblib", compress=3)
    ecrire_json(run / "validation.json", scores)
    for nom, p in predictions.items():
        ecrire_csv(run / f"predictions_{nom}_validation.csv", p)
    meta = {"identite": ident, "retenu": paquet["retenu"], "dataset": str(data.resolve()),
        "validation": scores, "versions": {"python": platform.python_version(), "sklearn": sklearn.__version__,
                                           "numpy": np.__version__, "pandas": pd.__version__},
        "date_fin_suivi_train": yt.date_fin_suivi.max(), "date_fin_suivi_validation": yv.date_fin_suivi.max(),
        "actifs_train": train.actif_id.tolist(), "actifs_validation": val.actif_id.tolist(),
        "donnees_train_validation_sha256": {partie: json.loads((data / partie / "audit.json").read_text(encoding="utf-8"))["fichiers_sha256"]
                                           for partie in ["train", "validation"]},
        "artefacts_sha256": {f: sha256(run / f) for f in ["modele.joblib", "validation.json"]},
        "test_genere_ou_lu_pour_entrainement": False}
    ecrire_json(run / "gel.json", meta)
    (run / "_GELE").write_text("Modele et protocole figes avant test\n", encoding="utf-8")
    afficher("VALIDATION", scores, paquet["retenu"])
    print("PASS technique. Le modele apprend une loi inventee ; aucune qualite terrain mesuree.")
    print(f"Modele : {run}")
    return run


def dessiner(run, predictions, verite, retenu, scores):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    couleurs = {"constante_train": "#9ca3af", "age_seul": "#d97706", "logistique_actifs": "#087f8c"}
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.4))
    for nom, p in predictions.items():
        z = p.merge(verite, on=CLES, validate="one_to_one")
        precision, rappel, _ = precision_recall_curve(z.defaillance_30j, z.score_interne_non_calibre_terrain)
        axes[0].plot(rappel, precision, label=nom, color=couleurs[nom], linewidth=2, drawstyle="steps-post")
    axes[0].set(xlabel="Rappel des incidents futurs", ylabel="Precision", xlim=(0, 1), ylim=(0, 1), title="Classement sur le test synthetique")
    axes[0].legend(fontsize=9)
    noms = list(scores)
    axes[1].bar(noms, [scores[n]["rappel_quota"] for n in noms], color=[couleurs[n] for n in noms])
    axes[1].set(ylabel="Part des incidents futurs couverts", ylim=(0, 1), title="Inspection des 20 % premiers du lot")
    axes[1].tick_params(axis="x", labelsize=9)
    for i, n in enumerate(noms):
        v = scores[n]
        axes[1].text(i, v["rappel_quota"] + 0.025, f'{v["incidents_parmi_inspectes"]}/{v["incidents_futurs"]}', ha="center")
    for ax in axes:
        ax.grid(axis="y", alpha=0.2)
        ax.set_axisbelow(True)
    fig.suptitle("AQUA Sentinel — priorite d'inspection a 30 jours\nDonnees et incidents artificiels ; modele retenu : " + retenu, fontsize=13)
    fig.text(0.5, 0.018, "Ce test mesure une loi inventee. Aucun score ne valide la prediction de casses reelles.", ha="center", fontsize=10)
    fig.tight_layout(rect=(0, 0.05, 1, 0.9))
    fig.savefig(run / "bilan_risque_test.png", dpi=160)
    plt.close(fig)


def evaluer(run):
    run = Path(run).resolve()
    paquet, meta = charger_modele(run)
    if (run / "_TEST_EVALUE").exists():
        empreinte = (run / "_TEST_EVALUE").read_text(encoding="utf-8").strip()
        if sha256(run / "bilan_test.json") != empreinte:
            raise ValueError("Bilan de test modifie.")
        bilan = json.loads((run / "bilan_test.json").read_text(encoding="utf-8"))
        afficher("TEST DEJA EVALUE, AUCUN REGLAGE", bilan["resultats"], meta["retenu"])
        print(f"Resultats : {run}")
        return bilan
    data = DOSSIER / "data" / "aqua_risque_test_v1" / f"seed{GRAINE_TEST}_n2000"
    data.mkdir(parents=True, exist_ok=True)
    reservation = {"identite": identite(), "sha256_gel": sha256(run / "gel.json"),
                   "sha256_modele": sha256(run / "modele.joblib")}
    verrou = data / "reservation.json"
    if verrou.exists():
        if json.loads(verrou.read_text(encoding="utf-8")) != reservation:
            raise ValueError("Test deja reserve a un autre gel. Une version modifiee necessite un nouveau protocole et un nouveau test.")
    else:
        ecrire_json(verrou, reservation)
    preparer_partie(data, "test")
    x = lire_observations(data)
    anciens = set(meta["actifs_train"]) | set(meta["actifs_validation"])
    if anciens & set(x.actif_id) or not (pd.Timestamp(meta["date_fin_suivi_validation"]) < pd.to_datetime(x.date_observation).min()):
        raise ValueError("Test non separe des anciennes cohortes dans le temps ou par actif.")
    predictions = {}
    for nom in METHODES:
        predictions[nom] = predire(x, paquet, nom)
        ecrire_csv(run / f"predictions_{nom}_test.csv", predictions[nom])
    # Lire la verite pour les scores SEULEMENT apres les trois predictions.
    y = lire_verite(data, x)
    scores = {nom: metriques(p, y) for nom, p in predictions.items()}
    par_type = {typ: metriques(predictions[meta["retenu"]].loc[lambda z: z.type_actif.eq(typ)], y)
                for typ in ["conduite", "raccord"]}
    bilan = {"origine": ORIGINE, "horizon_jours": HORIZON, "quota_inspection": QUOTA,
        "retenu_avant_test": meta["retenu"], "resultats": scores, "retenu_par_type": par_type,
        "quota_applique_au_lot_entier_pas_a_chaque_type": True,
        "predictions_sauvees_avant_lecture_labels_evaluateur": True,
        "actifs_communs_entre_jeux": 0, "separation_chronologique": True,
        "date_observation_test": PARTIES["test"][1], "validation_terrain": False,
        "limite": "Loi et donnees artificielles. Pas de probabilite de casse reelle, ni de date de rupture predite."}
    ecrire_json(run / "bilan_test.json", bilan)
    ecrire_csv(run / "comparaison_test.csv", pd.DataFrame([dict(methode=n, **v) for n, v in scores.items()]))
    dessiner(run, predictions, y, meta["retenu"], scores)
    (run / "_TEST_EVALUE").write_text(sha256(run / "bilan_test.json") + "\n", encoding="utf-8")
    afficher("TEST FINAL, AUCUN REGLAGE", scores, meta["retenu"])
    print("PASS technique. Scores sur une loi inventee ; aucune validation de casse reelle.")
    print(f"Resultats : {run}")
    return bilan


def expliquer(x, paquet):
    m = paquet["modeles"][paquet["retenu"]]
    v = m.named_steps["variables"]
    termes = v.get_feature_names_out()
    contributions = v.transform(x) * m.named_steps["logistique"].coef_[0]
    # Contributions additives a l'echelle logit. Les numeriques sont centres
    # sur la moyenne de train, les categories sur la premiere categorie.
    raisons = []
    for c in contributions:
        positifs = [j for j in np.argsort(-c) if c[j] > 0.05][:3]
        mots = []
        for j in positifs:
            terme = termes[j].split("__", 1)[1]
            mots.append(LIBELLES.get(terme, terme.replace("_", " ")))
        raisons.append(" ; ".join(mots) if mots else "aucune contribution positive importante")
    return raisons


def illustrer(run):
    run = Path(run).resolve()
    paquet, meta = charger_modele(run)
    if not (run / "_TEST_EVALUE").exists():
        raise ValueError("Evaluer d'abord le modele fige avant de produire la demonstration.")
    if sha256(run / "bilan_test.json") != (run / "_TEST_EVALUE").read_text(encoding="utf-8").strip():
        raise ValueError("Bilan de test modifie.")
    x, historique, _ = creer_cohorte("demo")
    p = predire(x, paquet)
    details = x[CLES + ["age_ans", "classe_materiau", "etat_inspection", "reparations_passees"]].copy()
    details["contributions_positives_modele"] = expliquer(x, paquet)
    p = p.merge(details, on=CLES, validate="one_to_one")
    p["nature_conclusion"] = "priorite relative fictive, aucune casse reelle predite"
    out = run / "illustration_maquette"
    out.mkdir(exist_ok=True)
    ecrire_csv(out / "observations_fictives.csv", x)
    ecrire_csv(out / "historique_fictif_30j.csv.gz", historique, True)
    ecrire_csv(out / "priorites_fictives.csv", p)
    ecrire_json(out / "avertissement.json", {"origine": ORIGINE, "inventaire_reel_disponible": False,
        "seules_dimensions_conduites_reprises_maquette": True,
        "raccords_hypothetiques_aux_noeuds": True, "probabilite_reelle_estimee": False,
        "quota": "2 actifs sur 9 proposes par arrondi de 20 %, quel que soit leur niveau absolu",
        "explications": "contributions additives positives du modele, pas causes de casse demontrees"})
    print("\nILLUSTRATION : AGES, CATEGORIES, INSPECTIONS ET HISTORIQUES INVENTES")
    print(p[["actif_id", "type_actif", "indice_relatif_0_100", "rang_dans_lot", "inspection_prioritaire"]].round(1).to_string(index=False))
    print("Indice 90 != 90 % de risque. Le quota propose toujours 2 actifs sur 9 ; aucune casse annoncee.")
    print(f"Illustration : {out}")
    return out


def main():
    parser = argparse.ArgumentParser(description="Risque futur AQUA : prototype sur actifs et incidents artificiels uniquement.")
    parser.add_argument("action", choices=["entrainer", "evaluer", "illustrer"])
    parser.add_argument("--modele", type=Path, help="Dossier d'un modele risque V1 deja fige.")
    args = parser.parse_args()
    if args.action == "entrainer":
        if args.modele:
            parser.error("--modele s'utilise seulement pour evaluer ou illustrer.")
        entrainer()
    elif args.action == "evaluer":
        evaluer(args.modele or dernier_modele())
    else:
        illustrer(args.modele or dernier_modele())


if __name__ == "__main__":
    main()
