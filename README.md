# Lido Missed-Block Monitor

Ein einzelnes Python-Skript ohne Abhängigkeiten. Es meldet in einem Telegram-Channel, wenn ein Validator des Lido Node Operators (Standard: #17 Solstice) seinen Block-Slot verpasst. Jede Meldung trägt einen Schweregrad.

## Funktionsweise

1. **Keys:** Das Skript liest alle deponierten Keys direkt aus dem Lido NodeOperatorsRegistry-Contract über eure Execution-Layer-Nodes (`EXECUTION_URLS`, mit Failover). Die öffentliche Lido Keys API dient nur als Notfall-Quelle (`KAPI_FALLBACK`). Die Keys werden alle 6 Stunden neu geladen.
2. **Validatoren:** Die Beacon Nodes (`BEACON_URLS`, mit Failover) ordnen die Pubkeys den Validator-Indizes zu. Nur Validatoren mit Status `active_*` oder `pending_*` werden berücksichtigt.
3. **Prüfung:** Jede Proposer-Duty eines eigenen Validators wird geprüft, sobald sie `CONFIRM_SLOTS` Slots alt ist. Ein Slot gilt nur dann als verpasst, wenn **keiner** der erreichbaren Beacon Nodes einen kanonischen Block dafür kennt. Ein einzelner Node, der hinterherhinkt, löst also keinen Fehlalarm aus.

## Schweregrade

| Stufe | Bedingung (Standard) | Telegram |
|---|---|---|
| ⚠️ WARNUNG | einzelner verpasster Block | stumm (`WARN_SILENT`) |
| 🔴 KRITISCH | ≥ 2 Duties **in Folge** verpasst (`CRIT_STREAK`) | mit Ton |
| 🔴 KRITISCH | ≥ 3 der letzten 10 Duties verpasst (`CRIT_WINDOW_MISSES` / `CRIT_WINDOW`) | mit Ton |
| ✅ ENTWARNUNG | erster Block nach einer kritischen Serie | mit Ton |

„In Folge“ bezieht sich auf die Duties des ganzen Operators, nicht auf einen einzelnen Validator. Bei ca. 7.800 Validatoren kommt etwa alle 20–30 Minuten eine Duty. Zwei verpasste Duties hintereinander deuten deshalb auf ein systematisches Problem hin, z. B. Relay oder MEV-Boost, Beacon Node, Signer oder Netzwerk. Die Fenster-Regel erkennt zusätzlich Fehler, die nur zeitweise auftreten.

Hat derselbe Validator in den letzten 30 Tagen schon öfter verpasst, steht das zusätzlich in der Meldung. Das hilft, eine einzelne defekte VC-Instanz zu finden.

Zusätzlich:
- 📊 täglicher Bericht (stumm)
- 🟠 Alarm, wenn die Beacon- oder EL-Nodes länger als 5 Minuten nicht erreichbar sind

## Setup mit Docker

Voraussetzung: Docker und das Docker-Compose-Plugin.

```bash
git clone https://github.com/chrsauer/lidomonitor.git
cd lidomonitor
cp .env.example .env
nano .env          # Nodes, Telegram-Token und Chat-ID eintragen
chmod 600 .env

docker compose build

# Prüfen: liefern alle EL-Nodes dieselben Keys wie die Lido Keys API?
docker compose run --rm lidomonitor check-keys

# Telegram testen
docker compose run --rm lidomonitor test-telegram

# Starten
docker compose up -d
docker compose logs -f
```

**Aktualisieren:**

```bash
git pull && docker compose up -d --build
```

**Zum Netzwerk:** Der Container läuft im Host-Netzwerk (`network_mode: host`). Nodes auf demselben Server erreicht er daher direkt über `localhost`, z. B. `http://localhost:8545`. Nodes auf anderen Servern werden ganz normal über IP oder Hostnamen eingetragen. Der Monitor öffnet selbst keine Ports.

**Zustand:** `state.json` liegt im Docker-Volume `lidomonitor-data` und übersteht Neustarts und Updates. Docker meldet den Container als `unhealthy`, wenn er länger als 5 Minuten keinen Slot verarbeitet hat.

Nach einem Neustart werden verpasste Slots nachgeholt, solange ein Beacon Node noch Duties für diese Epochs liefert. Lighthouse und Teku können das auch für vergangene Epochs, Lodestar nur für die aktuelle und die nächste.

## Telegram einrichten

1. **Bot anlegen:** In Telegram `@BotFather` öffnen, `/newbot` senden, einen Namen vergeben. Den angezeigten Token (Form `123456789:AAH…`) als `TELEGRAM_BOT_TOKEN` in `.env` eintragen.
2. **Bot in den Channel aufnehmen:** Channel → Verwalten → Administratoren → Administrator hinzufügen → Bot suchen. Er braucht das Recht „Nachrichten senden“.
3. **Chat-ID herausfinden:** Im Channel irgendeine Nachricht posten, dann ausführen:
   ```bash
   docker compose run --rm lidomonitor get-chat-id
   ```
   Ausgabe z. B. `TELEGRAM_CHAT_ID=-1001234567890    (channel: Solstice Alerts)`. Diese Zeile in `.env` übernehmen.

   Ohne Docker geht es auch im Browser: `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen und bei `"chat":{"id":-100…}` die ID ablesen.

   Bei einem öffentlichen Channel reicht stattdessen auch `@channelname`.
4. **Testen:**
   ```bash
   docker compose run --rm lidomonitor test-telegram
   ```

## Beispiel-Meldungen
```
⚠️ WARNUNG: Block verpasst – Lido Operator 17
einzelner verpasster Block
Validator: 1234567
Slot: 15392184 (Epoch 480993)
...

🔴 KRITISCH: Block verpasst – Lido Operator 17
2 Duties in Folge verpasst
...

✅ ENTWARNUNG – Lido Operator 17
Wieder Block produziert nach 3 verpassten Duties.
```
