# Résumé des modifications

## Objectif
Améliorer la gestion des posts avec contenu non chargé et ajouter des logs plus détaillés pour le débogage.

## Modifications apportées

### 1. GramAddict/core/views.py
- **`_get_media_container()`**: Ajout d'une gestion d'erreur pour `get_desc()` qui peut lever une `JSONRPCError` si le contenu n'est pas chargé
- **`_log_media_type()`**: Ajout d'un log de debug pour voir le contenu de la description récupéré
- **`detect_media_type()`**: Ajout d'un log de debug pour les appels à la fonction de détection
- **`_like_in_post_view()`**: Ajout d'un log de debug pour le contenu
- **`OpenedPostView`**: Ajout de log pour le type de média inconnu et amélioration de la gestion du type de média inconnu

### 2. GramAddict/plugins/core_arguments.py
- Ajout du paramètre `--use-ocr` pour utiliser l'OCR (pytesseract) pour détecter le nom d'utilisateur quand les méthodes normales échouent

### 3. GramAddict/plugins/like_from_urls.py
- Ajout de logs de debug pour le contenu de la description récupéré
- Ajout d'un warning si le contenu de la description est None

### 4. GramAddict/core/handle_sources.py
- Correction de l'import de `DeviceFacade` qui était manquant

### 5. GramAddict/core/filter.py
- Ajout du champ `skip_bio_check` pour ignorer la vérification de la bio si nécessaire

### 6. GramAddict/core/utils.py
- Nettoyage d'un espace inutile dans le code

## Problèmes résolus

1. **Gestion d'erreur pour les descriptions vides**: Si le contenu du post n'est pas chargé, `get_desc()` peut lever une exception JSONRPCError. Maintenant cela est géré correctement.
2. **Logs de debug**: Ajout de logs pour voir ce qui se passe quand le contenu est récupéré
3. **Type de média inconnu**: Ajout de log pour les cas où le type de média ne peut pas être déterminé

## Tests recommandés

1. Lancer le bot avec les modifications et observer les logs de debug
2. Vérifier que le bot ne bloque pas quand le contenu n'est pas chargé
3. Tester avec différents types de posts (photo, vidéo, carousel)

## Commande de test

```bash
python main.py run --profile <nom_profile> --source feed --scrape-posts
```

## Remarques

- Les logs de debug sont activés par défaut avec le niveau DEBUG
- Si le problème persiste (4-6 minutes par post), cela peut être dû à:
  - Connexion internet lente
  - Instagram met en cache les posts
  - Timeout réseau