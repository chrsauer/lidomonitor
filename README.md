# Lido Missed-Block Monitor

Ein einzelnes Python-Skript ohne Abhängigkeiten. Es meldet in einem Telegram-Channel, wenn ein Validator des Lido Node Operators (Standard: #17 Solstice) seinen Block-Slot verpasst. Jede Meldung trägt einen Schweregrad.

## Funktionsweise

1. **Keys:** Das Skript liest alle deponierten Keys direkt aus dem Lido NodeOperatorsRegistry-Contract über eure Execution-Layer-Nodes (`EXECUTION_URLS`, mit Failover). Die öffentliche Lido Keys API dient nur als Notfall-Quelle (`KAPI_FALLBACK`). Die Keys werden alle 6 Stunden neu geladen.
2. **Validatoren:** Die Beacon Nodes (`BEACON_URLS`, mit Failover) ordnen die Pubkeys den Validator-Indizes zu. Nur Validatoren mit Status `active_*` oder `pending_*` werden berücksichtigt.
3. **Prüfung:** Jede Proposer-Duty eines eigenen Validators wird geprüft, sobald sie `CONFIRM_SLOTS` Slots alt ist. Ein Slot gilt nur dann als verpasst, wenn **keiner** der erreichbaren Beacon Nodes einen kanonischen Block dafür kennt. Ein einzelner Node, der hinterherhinkt, löst also keinen Fehlalarm aus.

## Schweregrade

Verpasste Blöcke werden **pro VC** gezählt. Welcher Validator zu welchem VC gehört, steht in der CSV `config/validators.csv` (siehe unten).

| Stufe | Bedingung (Standard) | Telegram |
|---|---|---|
| ⚠️ WARNUNG | ein VC verpasst einen Block, die vorige Duty dieses VC war erfolgreich | stumm (`WARN_SILENT`) |
| 🔴 KRITISCH | **derselbe VC** verpasst ≥ 2 seiner Duties in Folge (`CRIT_STREAK`) | mit Ton |
| ✅ ENTWARNUNG | dieser VC produziert danach wieder einen Block | mit Ton |

Verpassen verschiedene VCs jeweils einzeln einen Block, bleibt es bei Warnungen. Validatoren ohne Eintrag in der CSV werden einzeln betrachtet. Ihre Meldungen zeigen keine VC-Bezeichnung.

Hat derselbe Validator in den letzten 30 Tagen schon öfter verpasst, steht das zusätzlich in der Meldung.

## VC-Zuordnung (CSV)

Die Datei `config/validators.csv` neben der `docker-compose.yml` ordnet Pubkeys den internen VC-Bezeichnungen zu:

```
Address;Node
0x80029f0c…7f;10a
0x80121919…d0;10a
```

- Trennzeichen `;`, `,` oder Tab, Kopfzeile optional, UTF-8 mit oder ohne BOM.
- Die Datei wird schreibgeschützt nach `/config` gemountet. Änderungen erkennt der Monitor automatisch, ein Neustart ist nicht nötig.
- `config/` steht in `.gitignore`. Die Zuordnung landet also nicht im Repo.
- `check-keys` zeigt, wie viele Einträge zu Operator 17 gehören.

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
mkdir -p config && cp /pfad/zu/LidoVc.csv config/validators.csv
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
einzelner verpasster Block (VC 10a)
Validator: 349949 · VC 10a
Slot: 15392184 (Epoch 480993)
...

🔴 KRITISCH: VC 10a verpasst wiederholt – Lido Operator 17
VC 10a: 2 Duties in Folge verpasst
Validator: 349950 · VC 10a
...

✅ ENTWARNUNG: VC 10a – Lido Operator 17
Wieder Block produziert nach 3 verpassten Duties in Folge.
```
