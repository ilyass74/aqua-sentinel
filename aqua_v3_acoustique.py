"""Experience de correlation acoustique, distincte de la localisation V3.

Deux signaux synchronises, une distance et une vitesse d'onde declarees
sont indispensables. Aucun de ces signaux n'est present dans le dataset
historique. Les criteres de rejet sont des choix d'exercice non calibres.
Reference : Almeida et al., Sensors 2014, doi:10.3390/s140305595.
"""
import math
import numpy as np
from aqua_v3_monitor import digest


def correlate(data):
    if not data["synchronisation_confirmee"]:
        raise ValueError("Deux acquisitions synchronisees sont requises ; aucun decalage d'horloge n'est corrige automatiquement.")
    a, b = np.asarray(data["amont"],dtype=float), np.asarray(data["aval"],dtype=float)
    if len(a) != len(b) or not 32 <= len(a) <= 4096 or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise ValueError("Deux signaux finis de meme longueur, entre 32 et 4096 points, sont requis.")
    fs, length, speed = data["frequence_hz"], data["distance_capteurs_m"], data["vitesse_onde_m_s"]
    lagmax = math.floor(length/speed*fs)
    if lagmax < 1 or lagmax > len(a)//3:
        raise ValueError("Duree ou frequence d'acquisition insuffisante pour le retard physique maximal.")
    a, b = a-a.mean(), b-b.mean()
    norm = float(np.linalg.norm(a)*np.linalg.norm(b))
    result = {"origine":"experience_acoustique_simulee", "conduite":data["conduite"], "position_depuis_amont_m":None,
              "sha256_entree":digest(data), "frequence_hz":fs, "distance_capteurs_m":length, "vitesse_onde_m_s":speed,
              "resolution_echantillonnage_m":speed/(2*fs),
              "seuils_exercice":{"correlation_min":.35,"pic_secondaire_max_relatif":.85},
              "limite":"Experience a vitesse d'onde constante declaree, non calibree sur une conduite. Reflexions, filtrage, horloges et bruit peuvent deplacer le pic. Ne modifie pas le detecteur Bilans_13 ni sa localisation historique.",
              "source":"https://pmc.ncbi.nlm.nih.gov/articles/PMC4004009/"}
    if norm < 1e-12:
        return dict(result,etat="insuffisant",motif="Signal constant ou energie insuffisante.")
    corr = np.correlate(a,b,mode="full")/norm
    lags = np.arange(-lagmax,lagmax+1)
    values = np.abs(corr[len(a)-1+lags])
    best = int(np.argmax(values))
    lag, peak = int(lags[best]),float(values[best])
    outside = np.abs(lags-lag)>max(2,int(fs*.001))
    secondary = float(values[outside].max()) if outside.any() else 0.
    result.update(retard_amont_moins_aval_s=lag/fs,correlation_pic=peak,pic_secondaire=secondary,
                  convention="retard = arrivee_amont - arrivee_aval ; x=(distance+vitesse*retard)/2")
    if peak < .35 or secondary >= .85*peak or abs(lag) == lagmax:
        return dict(result,etat="ambigu",motif="Pic faible, concurrent ou a la limite physique ; aucune position avancee.")
    position = (length+speed*lag/fs)/2
    if not 0 <= position <= length:
        return dict(result,etat="ambigu",motif="Position hors de l'intervalle instrumente.")
    return dict(result,etat="estimation_experimentale",position_depuis_amont_m=position,
                motif="Pic exploitable selon les criteres d'exercice ; position conditionnelle a la vitesse et aux horloges fournies.")
