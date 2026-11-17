# AQUA Sentinel - plateforme V3

Cette mise a jour applique au projet les fonctions logicielles manquantes du document de vision. Elle conserve les codes, donnees, modeles et resultats historiques. La couche operationnelle est nouvelle ; **tout reste une maquette sur donnees simulees**.

## Telechargement et installation Windows

Prerequis : le projet V1 qui fonctionnait deja dans `C:\Users\HP\Documents\aqua-sentinel`, avec `.venv`, les modeles et les donnees. **Il n'est pas necessaire d'avoir installe le ZIP V2** : ses huit fichiers de dependance sont inclus et ajoutes s'ils manquent. Un fichier V2 local different de la livraison connue est conserve ; l'installateur s'arrete avant toute copie pour eviter de perdre une modification.

1. Telecharger **AQUA_PLATEFORME_V3.zip**, ou l'enregistrer dans le dossier de son choix.
2. Ouvrir PowerShell et copier **tout** le bloc suivant. La fenetre permet de choisir le ZIP sans supposer qu'il est dans Downloads.

```powershell
& {
    $ErrorActionPreference = 'Stop'
    Set-Location -LiteralPath 'C:\Users\HP\Documents\aqua-sentinel'

    Add-Type -AssemblyName System.Windows.Forms
    $aquaChoixV3 = New-Object System.Windows.Forms.OpenFileDialog
    $aquaChoixV3.Title = 'Selectionne AQUA_PLATEFORME_V3.zip'
    $aquaChoixV3.Filter = 'Archives ZIP (*.zip)|*.zip'
    $aquaChoixV3.CheckFileExists = $true
    $aquaChoixV3.Multiselect = $false
    $aquaChoixV3.RestoreDirectory = $true
    try {
        if ($aquaChoixV3.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            return
        }
        $aquaArchiveV3 = $aquaChoixV3.FileName
    }
    finally {
        $aquaChoixV3.Dispose()
    }

    $aquaExtractionV3 = Join-Path $env:TEMP ('aqua_v3_' + [Guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $aquaArchiveV3 -DestinationPath $aquaExtractionV3

    $aquaInstallateurV3 = Join-Path $aquaExtractionV3 'aqua_installer_plateforme_v3.py'
    if (-not (Test-Path -LiteralPath $aquaInstallateurV3)) {
        throw 'Le ZIP selectionne ne contient pas l installateur de plateforme V3.'
    }

    .\.venv\Scripts\python.exe $aquaInstallateurV3 --projet (Get-Location).Path
    if ($LASTEXITCODE -ne 0) { throw 'Copie V3 interrompue.' }

    .\.venv\Scripts\python.exe -m pip install -r .\requirements_api_v3.txt
    if ($LASTEXITCODE -ne 0) { throw 'Installation des dependances interrompue.' }

    .\.venv\Scripts\python.exe .\aqua_api_v3.py verifier
    if ($LASTEXITCODE -ne 0) { throw 'Verification V3 interrompue.' }

    .\.venv\Scripts\python.exe .\aqua_api_v3.py servir --ouvrir
}
```

Adresse attendue : **http://127.0.0.1:8014**. Garder PowerShell ouvert. Ctrl+C arrete le serveur. Si ce port est occupe :

```powershell
.\.venv\Scripts\python.exe .\aqua_api_v3.py servir --port 8015 --ouvrir
```

Pour relancer plus tard, sans reinstaller :

```powershell
Set-Location -LiteralPath 'C:\Users\HP\Documents\aqua-sentinel'
.\.venv\Scripts\python.exe .\aqua_api_v3.py servir --ouvrir
```

L'installateur controle les empreintes des fichiers du paquet, ajoute les dependances V2 absentes et sauvegarde les precedents fichiers de plateforme V3 avant remplacement. Le manifeste verifie l'integrite de la livraison ; ce n'est pas une signature cryptographique d'editeur. La sauvegarde et le recu sont dans `resultats_aqua\sauvegardes_installation_v3`.

## Parcours pour decouvrir les ajouts

1. **Supervision** : ouvrir « Creer une nouvelle demonstration », choisir une journee avec signal et cliquer « Creer et recevoir 12 mesures ». La carte, les huit indicateurs et la fiche de secteur se remplissent a partir des mesures recues. Lecture continue / Pause pilotent la source serveur.
2. Cliquer P2, P3, P4 ou P5. Les filtres permettent d'afficher alertes, pressions critiques, signaux nocturnes, interventions et capteurs suspects. La selection est aussi accessible au clavier.
3. **Secteurs & nocturne** : consulter les contributions aux preuves, leur couverture, la reference journaliere et les points nocturnes. Une acoustique absente reste absente. Le minimum nocturne ne porte que sur les points deja recus. Les anciens scenarios n'ont pas de dates ; le profil hebdomadaire n'est renseigne qu'apres import d'un lot explicite.
4. **Etat des capteurs** : consulter les 13 mesures historiques et les quatre emplacements acoustiques optionnels. Un complement de simulation peut fournir acoustique, reference acoustique, niveau et etat de pompe pour le dernier point recu. Mettre la source en pause avant l'ajout. Un complement deja enregistre ne peut pas etre remplace par un autre contenu.
5. Pour essayer le contrat hebdomadaire, importer **AQUA_V3_EXEMPLE_HEBDOMADAIRE.json**. Ses quelques valeurs sont explicitement fictives ; elles ne constituent pas un profil reel complet. Pour la correlation, ouvrir « Experience de localisation acoustique » et importer **AQUA_V3_EXEMPLE_ACOUSTIQUE.json** : le resultat attendu est 40 m dans cette construction artificielle. Ce fichier n'est pas une acquisition de fuite reelle.
6. **Controle securise** : utiliser d'abord les conditions par defaut. Actualiser les conditions, proposer « Reduire VA de 20 points », consulter l'avant/apres et les pas de variation. Cocher l'approbation pour executer **dans le simulateur**, puis verifier le retour. Choisir « Vanne bloquee », « Reponse trop lente » ou « Retour capteur absent » pour observer le gel. Actualiser les conditions apres examen de l'equipement pour reprendre l'experience.
7. Tester communication indisponible, qualite insuffisante ou source trop faible : le moteur refuse les mouvements. La zone B est critique dans cet exercice ; son isolement demande autorisation explicite et justification. Le mode automatique permet uniquement un cycle d'equilibrage de laboratoire lance par le bouton ; il n'autorise pas d'isolement automatique. Les effectifs d'usagers restent inconnus tant qu'ils ne sont pas fournis.
8. **Interventions** : choisir un secteur, un responsable, une description et eventuellement une echeance. Passer de planifiee a en cours, puis a verifier. La cloture exige trois nouvelles mesures utilisables sans alerte apres la demande de verification. Un ancien signal actif peut donc empecher la cloture. Le retour d'un rejeu ne prouve pas une reparation physique.
9. **Assistant** : interroger les alertes, preuves, volumes, capteurs suspects et priorites d'inspection. Les sources renvoient aux mesures precises, au classement fictif ou au contexte du laboratoire. Une vanne inconnue comme V12 ne devient pas silencieusement VA. Demander la fermeture de VA ou VB calcule un scenario de lecture seule avec effets sur pression, fuite et desserte, puis fournit un lien pour calculer une proposition ; l'assistant ne cree ni n'execute de commande.
10. **Rapports & historique** : exporter JSON, CSV ou rapport HTML. Ouvrir le rapport HTML puis cliquer « Imprimer / enregistrer en PDF ». Le journal controle sa chaine locale de hachages. Les liens conservent les vues V2 de placement, de regulation, de comparaison d'interventions, d'inspection et de resultats historiques.

## Assistant avec LLM, en option

Par defaut, l'assistant est local et utilise des regles de selection : **aucune cle, aucun appel externe, pas de LLM**. Pour demander a Groq de selectionner les faits, definir volontairement dans le PowerShell du serveur :

```powershell
$aquaSecretGroq = Read-Host 'Cle API Groq' -AsSecureString
$env:AQUA_GROQ_API_KEY = [System.Net.NetworkCredential]::new('', $aquaSecretGroq).Password
$env:AQUA_GROQ_MODEL = Read-Host 'Identifiant du modele Groq compatible avec le mode JSON'
.\.venv\Scripts\python.exe .\aqua_api_v3.py servir --ouvrir
```

La saisie de la cle est masquee ; le code AQUA ne l'ecrit pas dans un fichier. Le code transmet alors la question et une selection de faits de simulation au service Groq. Aucun appel externe n'a ete effectue pendant nos essais. Le modele distant choisit uniquement des identifiants de faits ; le serveur refuse les identifiants inconnus, produit les phrases et les nombres a partir de ses preuves et revient au mode local en cas d'echec. Le nom du modele n'est pas pre-rempli : utiliser un modele effectivement disponible dans son compte et compatible avec le format JSON.

Pour revenir au mode local dans cette console :

```powershell
Remove-Item Env:AQUA_GROQ_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:AQUA_GROQ_MODEL -ErrorAction SilentlyContinue
```

## Persistance et limites des reseaux

- Flux, mesures et signaux V2 : base existante `resultats_aqua\extensions_v2\aqua_v2.sqlite3`, conservee. Ses sessions automatiques reprennent comme en V2.
- Operations V3, propositions, preuves, complements et audit : `resultats_aqua\plateforme_v3\aqua_v3.sqlite3`.
- `verifier` utilise des bases temporaires et ecrit son rapport dans un sous-dossier date de `resultats_aqua\plateforme_v3`. Il ne reinitialise pas le suivi.
- Le reseau historique R-A-B-C-D, les secteurs P2-P5 et les actifs fictifs d'inspection restent distincts du laboratoire de vannes A/B. Aucune correspondance inventee ne les fusionne.
- Le laboratoire simule des etats quasi-statiques. Les transitoires, coups de belier et temps de course reels ne sont pas calibres. Les vannes d'isolement sont binaires ideales ; la limite de 10 points par pas concerne les vannes modulantes.
- Les seuils de pression et de gravite, les poids de preuves, les criteres acoustiques et le seuil nocturne sont des parametres d'exercice. Ils ne sont pas des normes de conception ou d'exploitation d'un vrai reseau.

## Etat des verifications

Consulter **AQUA_V3_VERIFICATION.json**. Les 14 groupes de tests backend couvrent les calculs, les refus de commande, les retours defaillants, la persistance, les interventions et les rapports. Les tests de l'interface parcourent sept vues au moyen de jsdom et d'une API HTTP locale. Les fichiers historiques ont egalement ete compares avant/apres.

La verification a ete effectuee sous Linux avec Python 3.12. Les interactions jsdom **ne remplacent pas un controle visuel dans un navigateur Windows**. La commande `verifier` rejoue les tests backend sur le poste utilisateur. Le service Groq reel et les equipements de terrain n'ont pas ete testes. Aucun nouveau taux de detection ML ou de rupture reelle n'est annonce.

## Sources

La comparaison detaillee, page par page et fonction par fonction, est dans **AQUA_V3_COMPARAISON.md**. Les codes livrables et leurs empreintes sont dans le manifeste.

- Document fourni : AQUA_Sentinel_Vision_Plateforme(1).pdf, 13 pages.
- WNTR / US EPA : https://usepa.github.io/WNTR/hydraulics.html
- EPANET 2.2 / US EPA : https://usepa.github.io/EPANET2.2/3_network_model.html
- Almeida et al., Sensors 2014, correlation acoustique : https://pmc.ncbi.nlm.nih.gov/articles/PMC4004009/
- Groq : https://console.groq.com/docs/openai et https://console.groq.com/docs/structured-outputs
- Python SQLite : https://docs.python.org/3.13/library/sqlite3.html
- Microsoft OpenFileDialog : https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.openfiledialog
