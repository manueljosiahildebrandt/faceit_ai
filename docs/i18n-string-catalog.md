# faceit_ai i18n string catalog (EN → DE)

**Status (2026-07-17):** German (DE) translations are complete in `src/faceit_ai/i18n/strings.json`. Web UI wiring in `web_gui.py` is done for Settings, Analyze, Review, People pages (HTML + JS + help tips) and cookie-aware API JSON messages. Tk `gui.py` is not wired.

Source of truth for runtime: `src/faceit_ai/i18n/strings.json`.
Optional: wire Tk `gui.py`. Keep English source text unchanged unless fixing typos.
When adding keys, update both this catalog and `strings.json`.

**Languages:** `en` (English), `de` (Deutsch)  
**Preference cookie:** `facit_lang=en|de` (set by the header language toggle)  
**Wiring:** web pages + API JSON via `src/faceit_ai/i18n/__init__.py` (`t`, bootstrap `window.t`)

Format per row: `key` | `en` | `de` — DE filled in `strings.json`; keep this doc in sync when adding keys.

---

## Shared / Nav / Header

| key | en | de |
|-----|----|----|
| app.title.analyze | faceit_ai web UI - Analyze | faceit_ai Web-Oberfläche - Analysieren |
| app.title.review | faceit_ai web UI - Review | faceit_ai Web-Oberfläche - Prüfung |
| app.title.people | faceit_ai web UI - People | faceit_ai Web-Oberfläche - Personen |
| app.title.settings | faceit_ai web UI - Settings | faceit_ai Web-Oberfläche - Einstellungen |
| app.heading | faceit_ai web UI | faceit_ai Web-Oberfläche |
| app.version_label | Version: {version} | Version: {version} |
| app.version_tip | Package version from pyproject.toml | Paketversion aus pyproject.toml |
| nav.analyze | Analyze | Analysieren |
| nav.review | Review | Prüfung |
| nav.people | People | Personen |
| nav.settings | Settings | Einstellungen |
| nav.stop_server | Stop faceit_ai | faceit_ai stoppen |
| nav.badge.idle | Idle | Bereit |
| nav.lang_aria | Language | Sprache |
| nav.lang_de | DE | DE |
| nav.lang_en | EN | EN |
| nav.lang_de_title | Deutsch | Deutsch |
| nav.lang_en_title | English | English |
| nav.people_mismatch_tip | Some people have photos ≠ embeddings — use Re-register. | Bei einigen Personen stimmen Fotos und Embeddings nicht überein — Re-Registrierung verwenden. |
| subtitle.analyze | Analyze photos and metadata sync. | Fotos analysieren und Metadaten synchronisieren. |
| subtitle.review | Browse Review and Blocked photos; assign faces and confirm decisions. | Prüfungs- und gesperrte Fotos durchsehen; Gesichter zuweisen und Entscheidungen bestätigen. |
| subtitle.people | People folder is the source of truth. Scan syncs the database to match. | Der Personenordner ist die Quelle der Wahrheit. Scan gleicht die Datenbank ab. |
| subtitle.settings | Tune detection and metadata write parameters. | Erkennung und Metadaten-Parameter einstellen. |
| common.choose_finder | Choose in Finder | Im Finder auswählen |
| common.clear | Clear | Leeren |
| common.close | Close | Schließen |
| common.preview_alt | preview | Vorschau |
| common.not_found | Not Found | Nicht gefunden |
| common.bad_request | Bad Request | Ungültige Anfrage |
| common.done | Done. | Fertig. |
| common.failed | Failed. | Fehlgeschlagen. |
| common.request_failed | Request failed. | Anfrage fehlgeschlagen. |
| osascript.pick_folder | Select folder for faceit_ai web UI | Ordner für faceit_ai Web-Oberfläche auswählen |

---

## Analyze

| key | en | de |
|-----|----|----|
| analyze.legend | Analyze Photos | Fotos analysieren |
| analyze.label.source | Source | Quelle |
| analyze.placeholder.source | /path/to/source-folder | /pfad/zum/quellordner |
| analyze.label.destination | Destination | Ziel |
| analyze.placeholder.destination | /path/to/destination-folder | /pfad/zum/zielordner |
| analyze.btn.start | Start Analysis | Analyse starten |
| analyze.btn.stop | Stop Analysis | Analyse stoppen |
| analyze.status.legend | Current Status | Aktueller Status |
| analyze.metric.files_found | Files found | Dateien gefunden |
| analyze.metric.newly_analyzed | Newly analyzed | Neu analysiert |
| analyze.metric.unreadable | Unreadable skipped | Nicht lesbar (übersprungen) |
| analyze.metric.blocked | Blocked | Gesperrt |
| analyze.metric.review | Review | Prüfung |
| analyze.metric.ok | OK | OK |
| analyze.progress.title | Progress | Fortschritt |
| analyze.active_runs.title | Machines running now (shared database) | Aktuell laufende Rechner (gemeinsame Datenbank) |
| analyze.active_runs.empty | No machines are analyzing right now. | Aktuell analysiert kein Rechner. |
| analyze.activity.legend | Activity Feed | Aktivitätsverlauf |
| analyze.log.legend | Technical Log | Technisches Protokoll |
| analyze.log.copy | Copy log | Protokoll kopieren |
| analyze.log.copied | Copied | Kopiert |
| analyze.log.show | Show technical output | Technische Ausgabe anzeigen |
| analyze.alert.no_folder | Choose a folder to analyze first. | Bitte zuerst einen zu analysierenden Ordner auswählen. |
| analyze.alert.no_dest | Enter an archive destination (NAS path) for this folder. | Bitte ein Archivziel (NAS-Pfad) für diesen Ordner angeben. |
| analyze.confirm.stop_run | Stop the current run? Processed photos will still be checked out (flagged export / people folders). | Aktuellen Durchlauf stoppen? Bereits verarbeitete Fotos werden trotzdem übernommen (Export markierter Fotos / Personenordner). |
| analyze.alert.stop_failed | Could not stop the run | Durchlauf konnte nicht gestoppt werden |
| analyze.alert.copy_failed | Could not copy log to clipboard | Protokoll konnte nicht in die Zwischenablage kopiert werden |
| analyze.stage.preparing_ellipsis | Preparing… | Wird vorbereitet… |

---

## Review

| key | en | de |
|-----|----|----|
| review.folder.legend | Folder | Ordner |
| review.folder.placeholder | /path/to/shoot/folder | /pfad/zum/shooting-ordner |
| review.btn.load | Load photos | Fotos laden |
| review.tab.review | Review | Prüfung |
| review.tab.blocked | Blocked | Gesperrt |
| review.btn.batch_blocked | Batch move to Blocked | Alle nach Gesperrt verschieben |
| review.batch.hint | Moves all remaining Review photos using detected person names; unknown-only photos are skipped. | Verschiebt alle verbleibenden Prüfungsfotos anhand erkannter Personennamen; Fotos mit ausschließlich unbekannten Gesichtern werden übersprungen. |
| review.gallery.legend | Photos | Fotos |
| review.gallery.legend_dynamic | {kind} photos | {kind} Fotos |
| review.empty.choose | Choose a folder and load photos. | Ordner auswählen und Fotos laden. |
| review.empty.none | No {kind} photos for this folder. | Keine {kind} Fotos für diesen Ordner. |
| review.modal.title | Photo | Foto |
| review.modal.title_fallback | {kind} photo | {kind} Foto |
| review.nav.prev | ← Previous | ← Zurück |
| review.nav.next | Next → | Weiter → |
| review.btn.move_blocked | Move to blocked | Nach Gesperrt verschieben |
| review.btn.move_ok | Move to OK | Nach OK verschieben |
| review.btn.move_ok_unknown | Move to OK (unknown OK) | Nach OK verschieben (unbekannt OK) |
| review.meta.choose_folder | Choose a folder first. | Bitte zuerst einen Ordner auswählen. |
| review.meta.loading | Loading… | Wird geladen… |
| review.meta.count | {n} {kind} photo(s) in folder. | {n} {kind} Foto(s) im Ordner. |
| review.meta.load_failed | Could not load {label} photos. | {label}-Fotos konnten nicht geladen werden. |
| review.meta.photo_failed | Could not load photo. | Foto konnte nicht geladen werden. |
| review.meta.missing_disk | — missing on disk |   – fehlt auf der Festplatte |
| review.thumb.missing | (missing) |   (fehlt) |
| review.face.unknown | Unknown | Unbekannt |
| review.face.unknown_face | Unknown face | Unbekanntes Gesicht |
| review.face.detected | Detected: {name} | Erkannt: {name} |
| review.face.score | score {n} |   Score {n} |
| review.face.heading | Face #{id} | Gesicht #{id} |
| review.person.unknown_option | — Unknown person — | — Unbekannte Person — |
| review.person.not_in_folder | not in folder | nicht im Ordner |
| review.person.search_placeholder | Search people… | Person suchen… |
| review.person.add_to_folder | Add to people folder | Zum Personenordner hinzufügen |
| review.person.add_new | + Add new person… | + Neue Person… |
| review.person.add_title | Add new person | Neue Person anlegen |
| review.person.add_submit | Create and select | Anlegen und auswählen |
| review.meta.processing | Processing… | Wird verarbeitet… |
| review.meta.batch_processing | Batch processing… | Stapelverarbeitung… |
| review.alert.move_ok_failed | Could not move photo to OK. | Foto konnte nicht nach OK verschoben werden. |
| review.alert.confirm_blocked_failed | Could not confirm review photo. | Prüfungsfoto konnte nicht bestätigt werden. |
| review.alert.batch_failed | Batch move failed. | Stapelverschiebung fehlgeschlagen. |
| review.meta.batch_failed | Batch failed. | Stapelverarbeitung fehlgeschlagen. |
| review.alert.assign_required | Assign at least one face to a known person (not Unknown). | Mindestens ein Gesicht einer bekannten Person zuweisen (nicht Unbekannt). |
| review.confirm.ok_from_blocked | Move this photo to OK? (clears blocked status; flagged copies on disk are left as-is) | Dieses Foto nach OK verschieben? (hebt den gesperrten Status auf; markierte Kopien auf der Festplatte bleiben unverändert) |
| review.confirm.ok_from_review | Move this photo to OK? Unknown/stranger faces will be accepted for publishing. | Dieses Foto nach OK verschieben? Unbekannte/fremde Gesichter werden zur Veröffentlichung freigegeben. |
| review.confirm.blocked | Move to blocked and add reference crops for:\n{summary} | Nach Gesperrt verschieben und Referenzausschnitte hinzufügen für:\n{summary} |
| review.confirm.batch | Move all {n} review photo(s) with detected person names to Blocked?\n\nPhotos with only unknown faces are skipped. | Alle {n} Prüfungsfoto(s) mit erkannten Personennamen nach Gesperrt verschieben?\n\nFotos mit ausschließlich unbekannten Gesichtern werden übersprungen. |
| review.alert.skipped_prefix | Skipped: | Übersprungen: |
| review.alert.errors_prefix | Errors: | Fehler: |

---

## People / Gallery

| key | en | de |
|-----|----|----|
| people.folder.legend | People Folder | Personenordner |
| people.folder.placeholder | /path/to/people/root | /pfad/zum/personen-stammordner |
| people.scan.hint | Scan adds missing names, soft-removes names no longer in the folder (keeps embeddings for later), and leaves matches unchanged. Soft-remove stops matching so photos can be published. Use Wipe embeddings only for a hard clear. | Der Scan fügt fehlende Namen hinzu, entfernt Namen, die nicht mehr im Ordner vorhanden sind, weich (Embeddings bleiben für später erhalten), und lässt Übereinstimmungen unverändert. Weiches Entfernen stoppt den Abgleich, damit Fotos veröffentlicht werden können. „Embeddings löschen" nur für eine endgültige Bereinigung verwenden. |
| people.btn.scan | Scan People Folder | Personenordner scannen |
| people.list.title | People in this folder | Personen in diesem Ordner |
| people.btn.add | Add person… | Person hinzufügen… |
| people.search.placeholder | Search people… | Personen suchen… |
| people.search.empty | No people match your search. | Keine Personen entsprechen der Suche. |
| people.col.name | Name | Name |
| people.col.photos | Photos | Fotos |
| people.col.faces | Faces indexed | Erfasste Gesichter |
| people.col.faces_help | How many of this person's photos have been processed into a recognizable face profile. | Wie viele Fotos dieser Person zu einem erkennbaren Gesichtsprofil verarbeitet wurden. |
| people.col.consent | Consent | Einwilligung |
| people.col.tags | Tags | Tags |
| people.empty.no_folder | Choose a people folder above. The overview shows only names that exist as subfolders in that folder. | Oben einen Personenordner auswählen. Die Übersicht zeigt nur Namen, die als Unterordner in diesem Ordner vorhanden sind. |
| people.empty.no_subfolders | No person subfolders found in this folder. | Keine Personen-Unterordner in diesem Ordner gefunden. |
| people.status.registered | Registered | Registriert |
| people.status.not_registered | Not registered | Nicht registriert |
| people.status.no_photos | No photos in folder | Keine Fotos im Ordner |
| people.status.needs_scan | Needs scan (embeddings kept) | Scan erforderlich (Embeddings bleiben erhalten) |
| people.consent.allowed | Allowed | Erlaubt |
| people.consent.blocked | Blocked | Gesperrt |
| people.consent.click_to_block | Click to block | Klicken zum Sperren |
| people.consent.click_to_allow | Click to allow | Klicken zum Erlauben |
| people.mismatch.tip | {n} photos, {m} embeddings — re-register needed. | {n} Fotos, {m} Embeddings — Re-Registrierung erforderlich. |
| people.mismatch.aria | Needs re-register | Re-Registrierung erforderlich |
| people.menu.edit | Edit person… | Person bearbeiten… |
| people.menu.reregister | Re-register | Re-registrieren |
| people.menu.register | Register | Registrieren |
| people.menu.wipe | Wipe embeddings | Embeddings löschen |
| people.menu.actions_aria | Actions for {display} | Aktionen für {display} |
| people.dash | — | — |
| people.modal.add_title | Add person | Person hinzufügen |
| people.modal.edit_title | Edit person | Person bearbeiten |
| people.label.vorname | Vorname | Vorname |
| people.label.nachname | Nachname | Nachname |
| people.label.display_name | Display name (optional) | Anzeigename (optional) |
| people.placeholder.display_name | Auto from Vorname + Nachname | Automatisch aus Vorname + Nachname |
| people.slug.prefix | Folder: | Ordner: |
| people.label.consent | Consent | Einwilligung |
| people.label.photos | Photos | Fotos |
| people.btn.create | Create person | Person erstellen |
| people.btn.save | Save changes | Änderungen speichern |
| people.msg.creating | Creating person… | Person wird erstellt… |
| people.msg.saving | Saving person… | Person wird gespeichert… |
| people.msg.request_failed | Request failed: {e} | Anfrage fehlgeschlagen: {e} |
| people.tag.remove_aria | Remove tag | Tag entfernen |
| people.tag.consent_tip | Click to change consent status for {tag}. | Klicken, um den Einwilligungsstatus für {tag} zu ändern. |
| people.tag.picker_empty | No existing tags left — | Keine vorhandenen Tags mehr — |
| people.tag.new_placeholder | New tag | Neuer Tag |
| people.tag.add_title | Add | Hinzufügen |
| people.alert.tag_failed | Tag update failed. | Tag-Aktualisierung fehlgeschlagen. |
| people.msg.scanning | Scanning people folder... | Personenordner wird gescannt… |
| people.msg.scan_failed | Scan failed. | Scan fehlgeschlagen. |
| people.msg.updating_consent | Updating consent… | Einwilligung wird aktualisiert… |
| people.msg.update_failed | Update failed. | Aktualisierung fehlgeschlagen. |
| people.msg.wiping | Wiping embeddings… | Embeddings werden gelöscht… |
| people.msg.delete_failed | Delete failed. | Löschen fehlgeschlagen. |
| people.msg.reregistering | Re-registering {name}… | {name} wird re-registriert… |
| people.msg.reregister_failed | Re-register failed. | Re-Registrierung fehlgeschlagen. |
| people.msg.finished_refresh | Finished. Refreshing list… | Fertig. Liste wird aktualisiert… |
| people.confirm.wipe_short | Wipe all face data for "{name}"? This cannot be undone. | Alle Gesichtsdaten für „{name}" löschen? Dies kann nicht rückgängig gemacht werden. |
| people.confirm.wipe_long | Wipe embeddings for "{name}"? This permanently deletes face embeddings (photos on disk are kept). Person is deactivated and affected photos are re-labeled. Prefer folder sync for soft-remove if you may need them next year. | Embeddings für „{name}" löschen? Dadurch werden die Gesichts-Embeddings dauerhaft gelöscht (Fotos auf der Festplatte bleiben erhalten). Die Person wird deaktiviert und betroffene Fotos werden neu gekennzeichnet. Für ein weiches Entfernen den Ordnerabgleich bevorzugen, falls die Person nächstes Jahr eventuell wieder benötigt wird. |
| people.confirm.reregister | Wipe embeddings for "{name}" and re-scan only their folder photos? Other people are unchanged. | Embeddings für „{name}" löschen und nur die Fotos in ihrem Ordner neu scannen? Andere Personen bleiben unverändert. |
| people.gallery.title | Photos | Fotos |
| people.gallery.prev | ← Previous | ← Zurück |
| people.gallery.next | Next → | Weiter → |
| people.gallery.source_unknown | Source unknown | Quelle unbekannt |
| people.gallery.delete | Delete picture | Bild löschen |
| people.gallery.loading | Loading photos… | Fotos werden geladen… |
| people.gallery.count_hint | {n} photo(s) — click a thumbnail or use ← → arrow keys | {n} Foto(s) — Miniaturbild anklicken oder ← → Pfeiltasten verwenden |
| people.gallery.empty | No browser-viewable photos (JPEG/PNG/WebP) in this folder. | Keine im Browser anzeigbaren Fotos (JPEG/PNG/WebP) in diesem Ordner. |
| people.gallery.load_failed | Could not load photos. | Fotos konnten nicht geladen werden. |
| people.gallery.failed | Failed: {e} | Fehlgeschlagen: {e} |
| people.gallery.confirm_delete | Delete "{label}" from folder "{person}"? This only removes the file on disk. | „{label}" aus Ordner „{person}" löschen? Dies entfernt nur die Datei auf der Festplatte. |
| people.gallery.deleted | Deleted. | Gelöscht. |
| people.gallery.delete_failed | Delete failed. | Löschen fehlgeschlagen. |

---

## Settings

| key | en | de |
|-----|----|----|
| settings.section.data | Data & Database (multi-PC) | Daten & Datenbank (mehrere PCs) |
| settings.current_db | Current database | Aktuelle Datenbank |
| settings.data_folder | Data folder | Datenordner |
| settings.data_folder_help | Folder that holds local data (SQLite DB when no database URL is set, plus logs).\n\nPrecedence: FACEIT_AI_DATA_DIR env var > this value > current working directory. | Ordner, der lokale Daten enthält (SQLite-Datenbank, wenn keine Datenbank-URL gesetzt ist, sowie Protokolle).\n\nReihenfolge: Umgebungsvariable FACEIT_AI_DATA_DIR > dieser Wert > aktuelles Arbeitsverzeichnis. |
| settings.data_folder_placeholder | /path/to/data (leave empty for current folder) | /pfad/zu/daten (leer lassen für aktuellen Ordner) |
| settings.database_url | Database URL | Datenbank-URL |
| settings.database_url_help | Shared database connection for multiple PCs.\n\nLeave EMPTY to use the local SQLite file.\n\nPostgreSQL example:\npostgresql+psycopg://facit:${FACIT_DB_PASSWORD}@synology.local:5432/facit\n\n${ENV} placeholders are expanded, so the password can live in an environment variable instead of the file. | Gemeinsame Datenbankverbindung für mehrere PCs.\n\nLEER lassen, um die lokale SQLite-Datei zu verwenden.\n\nPostgreSQL-Beispiel:\npostgresql+psycopg://facit:${FACIT_DB_PASSWORD}@synology.local:5432/facit\n\n${ENV}-Platzhalter werden expandiert, sodass das Passwort in einer Umgebungsvariable statt in der Datei liegen kann. |
| settings.database_url_placeholder | empty = local SQLite; or postgresql+psycopg://user:pass@host:5432/facit | leer = lokales SQLite; oder postgresql+psycopg://user:pass@host:5432/facit |
| settings.btn.test_db | Test connection | Verbindung testen |
| settings.db.testing | Testing connection... | Verbindung wird getestet… |
| settings.db.ok | OK: connected to {backend}. | OK: verbunden mit {backend}. |
| settings.db.failed | Failed: {error} | Fehlgeschlagen: {error} |
| settings.db.hint | After changing the database URL, save settings, then run init_db once against the new database (and migrate_sqlite_to_db if you want to move existing data). | Nach Änderung der Datenbank-URL zuerst die Einstellungen speichern und dann einmalig init_db gegen die neue Datenbank ausführen (und migrate_sqlite_to_db, falls vorhandene Daten übernommen werden sollen). |
| settings.section.analyze | Analyze Settings | Analyse-Einstellungen |
| settings.group.reanalysis | Re-analysis | Neuanalyse |
| settings.force_reanalyze | Force re-analyze | Neuanalyse erzwingen |
| settings.force_reanalyze_help | Reprocess files even when already present in DB. Slower, but refreshes decisions. | Dateien erneut verarbeiten, auch wenn sie bereits in der Datenbank vorhanden sind. Langsamer, aktualisiert aber die Entscheidungen. |
| settings.force_reanalyze_hint | Ignore previously cached results and re-run detection on every photo. | Zwischengespeicherte Ergebnisse ignorieren und die Erkennung für jedes Foto erneut ausführen. |
| settings.group.archive | Archive copy (NAS) | Archivkopie (NAS) |
| settings.ingest_enable | Enable archive copy | Archivkopie aktivieren |
| settings.ingest_enable_help | When enabled, the Analyze page asks for a destination for each run. Source files are never deleted. | Wenn aktiviert, fragt die Analyse-Seite bei jedem Durchlauf nach einem Ziel. Quelldateien werden nie gelöscht. |
| settings.ingest_order | Order | Reihenfolge |
| settings.ingest_order_help | Copy first: analyze and flagged/ export on the archive copy. Analyze first: work on source, then copy everything (including flagged/) to archive. | Zuerst kopieren: Analyse und flagged/-Export erfolgen auf der Archivkopie. Zuerst analysieren: Arbeit erfolgt auf der Quelle, danach wird alles (einschließlich flagged/) ins Archiv kopiert. |
| settings.ingest_order.copy_first | Copy to archive, then analyze | Zuerst ins Archiv kopieren, dann analysieren |
| settings.ingest_order.analyze_first | Analyze source, then copy all to archive | Zuerst Quelle analysieren, dann alles ins Archiv kopieren |
| settings.ingest_order_hint | Set the destination path on the Analyze page for each folder. With "copy first", flagged/ is created on the archive copy. With "analyze first", flagged/ is on source and copied to archive afterward. | Das Ziel wird auf der Analyse-Seite für jeden Ordner festgelegt. Bei „Zuerst kopieren" wird flagged/ auf der Archivkopie angelegt. Bei „Zuerst analysieren" liegt flagged/ auf der Quelle und wird anschließend ins Archiv kopiert. |
| settings.group.flagged | Sort flagged photos (blocked/review) | Markierte Fotos sortieren (gesperrt/Prüfung) |
| settings.export_flagged | Export flagged files | Markierte Dateien exportieren |
| settings.export_flagged_help | What to do with flagged files after analysis (off / copy / move). | Was mit markierten Dateien nach der Analyse geschehen soll (aus / kopieren / verschieben). |
| settings.export.off | off | aus |
| settings.export.copy | copy | kopieren |
| settings.export.move | move | verschieben |
| settings.export.blocked | flagged/blocked | flagged/blocked |
| settings.export.blocked_help | Copy/move blocked photos into flagged/blocked/. | Gesperrte Fotos nach flagged/blocked/ kopieren/verschieben. |
| settings.export.review | flagged/review | flagged/review |
| settings.export.review_help | Copy/move Review photos (possible Blocked person, uncertain) into flagged/review/. | Prüfungsfotos (möglicherweise gesperrte Person, unsicher) nach flagged/review/ kopieren/verschieben. |
| settings.export_hint | Choose which categories get exported when analysis finds a match. | Auswählen, welche Kategorien exportiert werden, wenn die Analyse eine Übereinstimmung findet. |
| settings.group.people_folder | People folder | Personenordner |
| settings.crop_portraits | Crop portraits for people-folder collect | Porträts für Personenordner-Sammlung zuschneiden |
| settings.crop_portraits_help | When collecting strong matches to people folders, save face-centered portrait JPEGs instead of copying full scene files. | Beim Sammeln eindeutiger Treffer in Personenordnern werden gesichtszentrierte Porträt-JPEGs gespeichert statt vollständiger Szenendateien. |
| settings.crop_portraits_hint | Save a cropped face thumbnail into the People folder for each detected match. | Für jeden erkannten Treffer ein zugeschnittenes Gesichts-Thumbnail im Personenordner speichern. |
| settings.section.lightroom | Lightroom | Lightroom |
| settings.lr.enable | Enable Lightroom Meta Data | Lightroom-Metadaten aktivieren |
| settings.lr.enable_help | After analyze finishes, run metadata sync (Lightroom labels/keywords via ExifTool) for blocked/review photos. | Nach Abschluss der Analyse wird der Metadatenabgleich (Lightroom-Farbmarkierungen/Schlagwörter via ExifTool) für gesperrte/Prüfungsfotos ausgeführt. |
| settings.lr.labels_group | Lightroom Labels | Lightroom-Farbmarkierungen |
| settings.lr.blocked_label | Blocked label | Farbmarkierung für Gesperrt |
| settings.lr.blocked_label_help | Label applied when a non-consented person is detected. | Farbmarkierung, die gesetzt wird, wenn eine Person ohne Einwilligung erkannt wird. |
| settings.lr.review_label | Review label | Farbmarkierung für Prüfung |
| settings.lr.review_label_help | Label when a face might be a Blocked person (uncertain match) — needs a human check. | Farbmarkierung, wenn ein Gesicht möglicherweise einer gesperrten Person gehört (unsichere Übereinstimmung) — erfordert manuelle Prüfung. |
| settings.lr.ok_label | OK label | Farbmarkierung für OK |
| settings.lr.ok_label_help | Label applied when a photo is considered safe/ok. | Farbmarkierung, die gesetzt wird, wenn ein Foto als unbedenklich/ok gilt. |
| settings.lr.color.rot | Rot | Rot |
| settings.lr.color.gelb | Gelb | Gelb |
| settings.lr.color.gruen | Grün | Grün |
| settings.lr.color.blau | Blau | Blau |
| settings.lr.color.lila | Lila | Lila |
| settings.lr.color.none | None | Keine |
| settings.advanced | Advanced | Erweitert |
| settings.lr.verify | Verify metadata after writing | Metadaten nach dem Schreiben überprüfen |
| settings.lr.verify_help | Read file after writing metadata for verification. Safer but slower. | Datei nach dem Schreiben der Metadaten zur Überprüfung erneut lesen. Sicherer, aber langsamer. |
| settings.lr.exiftool_path | ExifTool path | ExifTool-Pfad |
| settings.section.ai | AI Model | KI-Modell |
| settings.ai.providers | Inference providers | Inferenz-Provider |
| settings.ai.providers_help | ONNX Runtime providers for InsightFace. auto (default): CUDA if installed, else DirectML on Windows (any DX12 GPU), else CPU. CoreML is skipped (incompatible with InsightFace SCRFD). Example: auto, DmlExecutionProvider, or CPUExecutionProvider | ONNX-Runtime-Provider für InsightFace. „auto“ (Standard): CUDA (falls installiert), sonst DirectML unter Windows (DX12-GPU), sonst CPU. CoreML wird übersprungen (nicht kompatibel mit InsightFace SCRFD). Beispiel: auto, DmlExecutionProvider oder CPUExecutionProvider |
| settings.ai.providers_placeholder | auto | auto |
| settings.ai.det_size | Face detection resolution | Auflösung der Gesichtserkennung |
| settings.ai.det_size_help | Detector input size (width,height).\n\nExamples:\n640,640 = higher quality, slower\n512,512 = balanced default\n400,400 = faster, may miss small/distant faces\n\nLow-end CPU: try 400-512\nHigh-end CPU/GPU: 512-640 is usually best. | Eingabegröße des Detektors (Breite,Höhe).\n\nBeispiele:\n640,640 = höhere Qualität, langsamer\n512,512 = ausgewogener Standard\n400,400 = schneller, kann kleine/entfernte Gesichter übersehen\n\nSchwache CPU: 400-512 ausprobieren\nStarke CPU/GPU: 512-640 ist meist am besten. |
| settings.ai.max_dimension | Maximum image size | Maximale Bildgröße |
| settings.ai.max_dimension_help | Largest image side used for analysis.\n\nExamples:\n2200 = more detail, slower\n1800 = balanced default\n1400 = faster, less detail for distant faces\n\nIf speed is priority: try 1400-1800\nIf small-face accuracy matters: try 1800-2200. | Längste für die Analyse verwendete Bildseite.\n\nBeispiele:\n2200 = mehr Detail, langsamer\n1800 = ausgewogener Standard\n1400 = schneller, weniger Detail bei entfernten Gesichtern\n\nWenn Geschwindigkeit Priorität hat: 1400-1800 ausprobieren\nWenn die Genauigkeit bei kleinen Gesichtern wichtig ist: 1800-2200 ausprobieren. |
| settings.ai.raw_half | Faster RAW processing | Schnellere RAW-Verarbeitung |
| settings.ai.raw_half_help | ON = half-size RAW decode (faster). OFF = full RAW decode (more detail, slower). | EIN = RAW-Dekodierung in halber Größe (schneller). AUS = vollständige RAW-Dekodierung (mehr Detail, langsamer). |
| settings.ai.debug | Debug logging | Debug-Protokollierung |
| settings.ai.config_preview | Config preview | Konfigurationsvorschau |
| settings.btn.save | Save settings | Einstellungen speichern |

---

## Common alerts / shutdown

| key | en | de |
|-----|----|----|
| common.alert.picker_failed | Could not open Finder folder picker | Finder-Ordnerauswahl konnte nicht geöffnet werden |
| common.confirm.stop_server | Stop faceit_ai web UI server? | faceit_ai-Webserver stoppen? |
| common.shutdown.title | Server stopped | Server gestoppt |
| common.shutdown.fallback_msg | Server stopping... | Server wird gestoppt… |
| common.shutdown.close_tab | You can close this tab. | Sie können diesen Tab schließen. |
| common.shutdown.close_tab_restart | You can close this tab. To start again, run faceit_ai_web in terminal. | Sie können diesen Tab schließen. Zum erneuten Start faceit_ai_web im Terminal ausführen. |

---

## Status / Activity / API (user-visible)

| key | en | de |
|-----|----|----|
| status.idle | Idle | Bereit |
| status.running | Running | Läuft |
| status.stopped | Stopped | Gestoppt |
| status.failed | Failed | Fehlgeschlagen |
| status.completed | Completed | Abgeschlossen |
| status.completed_warnings | Completed with warnings | Abgeschlossen mit Warnungen |
| stage.waiting | Waiting | Wartet |
| stage.preparing | Preparing | Wird vorbereitet |
| stage.finished | Finished | Abgeschlossen |
| stage.loading_models | Loading models | Modelle werden geladen |
| stage.copying_archive | Copying to archive | Wird ins Archiv kopiert |
| stage.finishing_analysis | Finishing analysis | Analyse wird abgeschlossen |
| stage.exporting_flagged | Exporting flagged photos | Markierte Fotos werden exportiert |
| stage.collecting_people | Collecting to people folders | Wird in Personenordner gesammelt |
| stage.writing_metadata | Writing metadata | Metadaten werden geschrieben |
| stage.analyzing | Analyzing photos | Fotos werden analysiert |
| stage.scanning_folder | Scanning folder | Ordner wird gescannt |
| stage.registering_people | Registering new people | Neue Personen werden registriert |
| progress.stopped | Stopped. | Gestoppt. |
| progress.finished | Finished. | Abgeschlossen. |
| progress.failed | Failed. | Fehlgeschlagen. |
| activity.starting | Starting: {task} | Start: {task} |
| activity.finished | Finished: {status} | Abgeschlossen: {status} |
| activity.found_photos | Found {n} photos in folder. | {n} Fotos im Ordner gefunden. |
| activity.analysis_finished | Analysis finished: {n} analyzed. Unreadable skipped: {d}. | Analyse abgeschlossen: {n} analysiert. Nicht lesbar übersprungen: {d}. |
| activity.metadata_sync | Metadata sync finished. Updated: {s}, skipped (no DB match / wrong status): {k}, errors: {e}. | Metadatenabgleich abgeschlossen. Aktualisiert: {s}, übersprungen (keine DB-Übereinstimmung / falscher Status): {k}, Fehler: {e}. |
| activity.stopped_early_checkout | Stopped early — running checkout (flagged export / people collect)… | Vorzeitig gestoppt — Abschlussverarbeitung läuft (Export markierter Fotos / Personensammlung)… |
| activity.stop_requested | Stop requested — finishing current file, then checkout (flagged / people folders)… | Stopp angefordert — aktuelle Datei wird abgeschlossen, danach Abschlussverarbeitung (markierte Fotos / Personenordner)… |
| activity.stopped_checkout_done | Stopped early — checkout finished for photos processed so far. | Vorzeitig gestoppt — Abschlussverarbeitung für bisher verarbeitete Fotos abgeschlossen. |
| activity.preparing_run | Preparing analysis run... | Analysedurchlauf wird vorbereitet… |
| activity.registering_folder | Registering people from selected folder... | Personen aus ausgewähltem Ordner werden registriert… |
| activity.consent_toggle | Toggling consent for {name}… | Einwilligung für {name} wird umgeschaltet… |
| activity.consent_done | Done: consent set to {status}, metadata applied={n}, errors={e}. | Fertig: Einwilligung auf {status} gesetzt, Metadaten angewendet={n}, Fehler={e}. |
| activity.wipe_start | Wiping embeddings for {name}… | Embeddings für {name} werden gelöscht… |
| activity.wipe_done | Done: wiped embeddings for {name}, metadata applied={n}. | Fertig: Embeddings für {name} gelöscht, Metadaten angewendet={n}. |
| activity.reregister_wipe | Wiped embeddings for {name}; scanning folder photos only… | Embeddings für {name} gelöscht; es werden nur die Ordnerfotos gescannt… |
| activity.reregister_new | Registering {name} from folder… | {name} wird aus dem Ordner registriert… |
| activity.reregister_ok | Re-registered {name} from their folder. | {name} wurde aus dem Ordner re-registriert. |
| activity.reregister_fail | Re-register failed for {name}. | Re-Registrierung für {name} fehlgeschlagen. |
| activity.scan_skip_empty | Skipped {name}: no supported images in folder (add photos, then re-scan). | {name} übersprungen: keine unterstützten Bilder im Ordner (Fotos hinzufügen, dann erneut scannen). |
| activity.soft_remove | Soft-removing {name}: stop matching, keep embeddings, re-label photos… | {name} wird weich entfernt: Abgleich wird gestoppt, Embeddings bleiben erhalten, Fotos werden neu gekennzeichnet… |
| activity.reactivate | Reactivating {name} (embeddings already stored)… | {name} wird reaktiviert (Embeddings bereits gespeichert)… |
| activity.register_person | Registering {name} from folder… | {name} wird aus dem Ordner registriert… |
| activity.registered | Registered {name}. | {name} registriert. |
| activity.register_fail | Failed to register {name}. Check Technical Log for details. | Registrierung von {name} fehlgeschlagen. Details im technischen Protokoll. |
| activity.scan_summary | To add: {a}, empty (skipped): {e}, reactivate: {r}, remove: {m}, unchanged: {u}. | Hinzuzufügen: {a}, leer (übersprungen): {e}, reaktivieren: {r}, entfernen: {m}, unverändert: {u}. |
| activity.scan_empty_list | Empty folders need photos first: {names}. |   Leere Ordner benötigen zuerst Fotos: {names}. |
| activity.scan_done | Done: registered {ok}/{total}, skipped empty {e}[, failed: …]. | Fertig: {ok}/{total} registriert, {e} leer übersprungen[, fehlgeschlagen: …]. |
| api.shutdown | Shutting down server... | Server wird heruntergefahren… |
| api.picker_cancelled | Finder picker cancelled or failed ({code}). | Finder-Auswahl abgebrochen oder fehlgeschlagen ({code}). |
| api.no_run | No run in progress. | Kein Durchlauf aktiv. |
| api.job_running | A job is already running. Please wait for it to finish. | Ein Vorgang läuft bereits. Bitte auf dessen Abschluss warten. |
| api.missing_person | Missing person name. | Personenname fehlt. |
| api.people_not_configured | People folder is not configured. | Personenordner ist nicht konfiguriert. |
| api.choose_people_folder | Please choose a people folder first. | Bitte zuerst einen Personenordner auswählen. |
| api.invalid_people_folder | Invalid people folder: {path} | Ungültiger Personenordner: {path} |
| api.invalid_people_folder_short | Invalid people folder. | Ungültiger Personenordner. |
| api.no_folder_for_person | No folder for '{name}' under the people root. | Kein Ordner für „{name}" im Personen-Stammordner. |
| api.person_no_images | {name}: folder has no supported images. Add photos, then try again. | {name}: Ordner enthält keine unterstützten Bilder. Fotos hinzufügen und erneut versuchen. |
| api.reregister_msg | Re-registering {name} (wipe + rescan their folder)… | {name} wird re-registriert (Embeddings löschen + Ordner erneut scannen)… |
| api.wipe_msg | Wiping embeddings for {name}… | Embeddings für {name} werden gelöscht… |
| api.consent_msg | Updating consent for {name}… | Einwilligung für {name} wird aktualisiert… |
| api.scan_matches | {summary} Overview matches the folder. | {summary} Übersicht stimmt mit dem Ordner überein. |
| api.scan_empty_error | {summary} Put JPEG/PNG/RAW photos inside each person folder, then scan again. | {summary} JPEG/PNG/RAW-Fotos in jeden Personenordner legen, dann erneut scannen. |
| api.scan_syncing | {summary} Syncing… | {summary} Wird abgeglichen… |
| api.create_need_names | Vorname and Nachname are required. | Vorname und Nachname sind erforderlich. |
| api.create_exists | A person folder already exists: {path} | Ein Personenordner existiert bereits: {path} |
| api.create_ok | Created {display} as folder {slug} ({n} photo(s) saved). Run Re-register to index faces. | {display} als Ordner {slug} erstellt ({n} Foto(s) gespeichert). „Re-registrieren" ausführen, um Gesichter zu erfassen. |
| api.update_ok | Updated {display}. | {display} aktualisiert. |
| api.person_folder_missing | Person folder not found: {slug} | Personenordner nicht gefunden: {slug} |
| api.tags_updated | Tags updated. | Tags aktualisiert. |
| api.expected_multipart | Expected multipart form data. | Multipart-Formulardaten erwartet. |
| api.invalid_consent | Invalid consent request. | Ungültige Einwilligungsanfrage. |
| api.invalid_folder | Invalid or missing folder. | Ungültiger oder fehlender Ordner. |
| api.invalid_asset | Invalid asset id. | Ungültige Asset-ID. |
| api.photo_not_found | Photo not found. | Foto nicht gefunden. |
| api.invalid_faces | Invalid face assignments. | Ungültige Gesichtszuordnungen. |
| api.invalid_person_name | Invalid person name '{name}'. | Ungültiger Personenname „{name}". |
| api.blocked_ok | Marked blocked; {c} crop(s), {e} embedding(s) added. | Als gesperrt markiert; {c} Ausschnitt(e), {e} Embedding(s) hinzugefügt. |
| api.moved_ok | Moved to OK. | Nach OK verschoben. |
| api.moved_ok_unknown | Moved to OK (unknown faces accepted). | Nach OK verschoben (unbekannte Gesichter akzeptiert). |
| api.batch_none | No review photos in folder. | Keine Prüfungsfotos im Ordner. |
| api.gallery_deleted | Deleted {file} from {name}. | {file} von {name} gelöscht. |
| db.label.sqlite | SQLite (local file): {path} | SQLite (lokale Datei): {path} |
| db.label.server | Server: {masked_url} | Server: {masked_url} |
| db.label.unknown | unknown | unbekannt |
| api.settings_saved | Saved settings to {path} | Einstellungen gespeichert unter {path} |

---

## Desktop Tk GUI (optional)

| key | en | de |
|-----|----|----|
| tk.window_title | faceit_ai batch GUI | faceit_ai Batch-GUI |
| tk.tab.run | Run Batch | Batch ausführen |
| tk.tab.settings | Search/Metadata Settings | Such-/Metadaten-Einstellungen |
| tk.people.frame | People Folder (Register Missing) | Personenordner (Fehlende registrieren) |
| tk.btn.browse | Browse | Durchsuchen |
| tk.people.scan | Start Scan People Folder | Personenordner-Scan starten |
| tk.analyze.frame | Analyze + Auto Metadata Sync | Analyse + automatischer Metadatenabgleich |
| tk.analyze.start | Start Analyze Batch | Analyse-Batch starten |
| tk.log.frame | Live Log | Live-Protokoll |
| tk.log.clear | Clear Log | Protokoll leeren |
| tk.tune.reload | Reload from config | Aus Konfiguration neu laden |
| tk.tune.save | Save settings to YAML | Einstellungen in YAML speichern |

---

## Notes for the implementing AI

- ~380 web UI strings + ~45 desktop; placeholders like `{name}`, `{n}` must be preserved.
- Lightroom color names (`Rot`/`Gelb`/…) are Lightroom UI labels — translate carefully or keep German as-is for DE Lightroom users.
- `people.label.vorname` / `nachname` are already German; consider `First name` / `Last name` for EN.
- Prefer one module `faceit_ai.i18n` with `t(key, lang, **kwargs)` and load from this catalog or JSON generated from it.
- Language cookie: `facit_lang`; toggle already in header (`setLang`).
