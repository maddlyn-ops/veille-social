# Veille sociale

Relevé automatique quotidien des comptes TikTok et Instagram suivis, pour alimenter le tableau de bord « Veille Product Design ».

## Comment ça marche

1. Chaque matin, GitHub Actions lance `scrape.py`.
2. TikTok est relevé avec yt-dlp (aucun compte, aucune clé).
3. Instagram est relevé avec l'API Bright Data (palier gratuit), si le secret `BRIGHTDATA_API_KEY` existe.
4. Le résultat est enregistré dans `data/social.json`.

## Modifier les comptes suivis

Éditer `accounts.json` : une liste d'identifiants par réseau, sans le @.

## Ajouter la clé Bright Data

Dans le dépôt : Settings, Secrets and variables, Actions, New repository secret.
Nom : `BRIGHTDATA_API_KEY`. Valeur : la clé API créée dans le compte Bright Data.

## Lancer un relevé à la main

Onglet Actions, « Relevé social quotidien », bouton Run workflow.

## Lire le résultat

Pour chaque compte : `status` (ok ou erreur), `median` (médiane du compte) et la liste des posts.
`ratio` compare un post à la médiane du compte : 3 signifie trois fois mieux que d'habitude.
La mesure utilisée est les vues pour TikTok et les likes pour Instagram.
