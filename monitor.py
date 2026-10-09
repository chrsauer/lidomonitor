#!/usr/bin/env python3
"""
Lido Node Operator – Missed-Block-Monitor
==========================================
Liest alle deponierten Keys eines Lido Node Operators direkt aus dem
NodeOperatorsRegistry-Contract (über eigene Execution-Layer-Nodes, alternativ
Lido Keys API), ermittelt die Validator-Indizes über eigene Beacon Nodes und
prüft jede Proposer-Duty. Fehlt ein Block -> Telegram-Nachricht mit Schweregrad:

  ⚠️ WARNUNG   einzelner verpasster Block (z. B. Latenz, Pech beim Timing)
  🔴 KRITISCH  >= CRIT_STREAK verpasste Duties in Folge
               oder >= CRIT_WINDOW_MISSES verpasste in den letzten CRIT_WINDOW Duties

Nur Python-Standardbibliothek, keine Abhängigkeiten.
Konfiguration über Umgebungsvariablen oder eine .env-Datei neben dem Skript.
"""

import html
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.split(" #", 1)[0]  # Inline-Kommentare erlauben
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def env_list(name: str, default: str = "") -> list[str]:
    return [u.strip().rstrip("/") for u in os.environ.get(name, default).split(",") if u.strip()]


load_dotenv(BASE_DIR / ".env")

# Mehrere Nodes kommagetrennt -> automatisches Failover
BEACON_URLS = env_list("BEACON_URLS") or env_list("BEACON_URL", "http://localhost:5052")
EXECUTION_URLS = env_list("EXECUTION_URLS")

# Lido
OPERATOR_ID = int(os.environ.get("OPERATOR_ID", "17"))
# NodeOperatorsRegistry (Curated Module) auf Mainnet
NOR_ADDRESS = os.environ.get("NOR_ADDRESS", "0x55032650b14df07b85bF18A3a3eC8E0Af2e028d5")
KAPI_URL = os.environ.get("KAPI_URL", "https://keys-api.lido.fi").rstrip("/")
LIDO_MODULE_ID = int(os.environ.get("LIDO_MODULE_ID", "1"))
# Keys API als Notfall-Quelle, falls kein EL-Node erreichbar ist
KAPI_FALLBACK = os.environ.get("KAPI_FALLBACK", "true").lower() == "true"

# Telegram
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
WARN_SILENT = os.environ.get("WARN_SILENT", "true").lower() == "true"

# Schweregrade
CRIT_STREAK = int(os.environ.get("CRIT_STREAK", "2"))
CRIT_WINDOW = int(os.environ.get("CRIT_WINDOW", "10"))
CRIT_WINDOW_MISSES = int(os.environ.get("CRIT_WINDOW_MISSES", "3"))  # 0 = aus

# Sonstiges
CONFIRM_SLOTS = int(os.environ.get("CONFIRM_SLOTS", "3"))  # Reorg-Puffer
KEYS_REFRESH_SEC = int(os.environ.get("KEYS_REFRESH_SEC", str(6 * 3600)))
DAILY_SUMMARY_HOUR_UTC = os.environ.get("DAILY_SUMMARY_HOUR_UTC", "7")  # leer = aus
NOTIFY_PROPOSED = os.environ.get("NOTIFY_PROPOSED", "false").lower() == "true"
ALERT_AFTER_ERRORS_SEC = int(os.environ.get("ALERT_AFTER_ERRORS_SEC", "300"))
EXPLORER = os.environ.get("EXPLORER_URL", "https://beaconcha.in").rstrip("/")
STATE_FILE = Path(os.environ.get("STATE_FILE", str(BASE_DIR / "state.json")))

SLOTS_PER_EPOCH = 32
SECONDS_PER_SLOT = 12
SLOTS_PER_DAY = 7200
PUBKEY_LEN = 48

# Funktions-Selektoren NodeOperatorsRegistry
SEL_GET_NODE_OPERATOR = "9a56983c"  # getNodeOperator(uint256,bool)
SEL_GET_SIGNING_KEYS = "59e25c12"  # getSigningKeys(uint256,uint256,uint256)
KEYS_BATCH = int(os.environ.get("KEYS_BATCH", "500"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("monitor")


# --------------------------------------------------------------------------- #
# HTTP-Helfer
# --------------------------------------------------------------------------- #
class NotFound(Exception):
    pass


def http_json(url: str, data=None, timeout: int = 30):
    body = None
    headers = {"Accept": "application/json", "User-Agent": "lido-proposal-monitor"}
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        raise


class Pool:
    """Liste gleichwertiger Nodes. Fällt einer aus, wird der nächste genommen."""

    def __init__(self, name: str, urls: list[str]):
        self.name, self.urls, self.current = name, urls, 0

    def request(self, fn):
        last_err: Exception | None = None
        for i in range(len(self.urls)):
            idx = (self.current + i) % len(self.urls)
            try:
                result = fn(self.urls[idx])
                if idx != self.current:
                    log.warning("%s: Wechsel auf %s", self.name, self.urls[idx])
                    self.current = idx
                return result
            except NotFound:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("%s %s Fehler: %s", self.name, self.urls[idx], e)
                last_err = e
        raise last_err or RuntimeError(f"{self.name}: keine Nodes konfiguriert")


beacon_pool = Pool("Beacon", BEACON_URLS)
el_pool = Pool("Execution", EXECUTION_URLS)


def beacon(path: str, data=None):
    return beacon_pool.request(lambda base: http_json(f"{base}{path}", data=data))


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def telegram(text: str, silent: bool = False) -> None:
    if not TG_TOKEN or not TG_CHAT:
        log.warning("Telegram nicht konfiguriert, Nachricht:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    for attempt in range(3):
        try:
            http_json(url, data=payload, timeout=15)
            return
        except Exception as e:  # noqa: BLE001
            log.error("Telegram-Fehler (Versuch %d): %s", attempt + 1, e)
            time.sleep(3)


# --------------------------------------------------------------------------- #
# Keys laden
# --------------------------------------------------------------------------- #
def _u256(n: int) -> str:
    return f"{n:064x}"


def eth_call(data: str) -> bytes:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": NOR_ADDRESS, "data": "0x" + data}, "latest"],
    }

    def call(url):
        resp = http_json(url, data=payload, timeout=60)
        if "error" in resp:
            raise RuntimeError(f"eth_call: {resp['error']}")
        result = bytes.fromhex(resp["result"][2:])
        if not result:
            # Kein Contract-Code an dieser Adresse: Node nicht synchron
            # oder falsches Netzwerk -> nächsten Node versuchen
            raise RuntimeError(
                f"{url}: leere Antwort vom Lido-Contract "
                "(Node nicht synchron oder falsches Netzwerk?)"
            )
        return result

    return el_pool.request(call)


def fetch_pubkeys_el() -> list[str]:
    """Deponierte Keys direkt aus dem NodeOperatorsRegistry-Contract."""
    info = eth_call(SEL_GET_NODE_OPERATOR + _u256(OPERATOR_ID) + _u256(0))
    # (bool active, string name, address reward, uint64 vetted, uint64 exited,
    #  uint64 added, uint64 deposited)
    deposited = int.from_bytes(info[6 * 32 : 7 * 32], "big")
    if deposited == 0:
        raise RuntimeError(f"Operator {OPERATOR_ID} hat laut Contract 0 deponierte Keys")
    keys: list[str] = []
    for offset in range(0, deposited, KEYS_BATCH):
        limit = min(KEYS_BATCH, deposited - offset)
        raw = eth_call(
            SEL_GET_SIGNING_KEYS + _u256(OPERATOR_ID) + _u256(offset) + _u256(limit)
        )
        # Rückgabe: (bytes pubkeys, bytes signatures, bool[] used)
        ptr = int.from_bytes(raw[0:32], "big")
        length = int.from_bytes(raw[ptr : ptr + 32], "big")
        blob = raw[ptr + 32 : ptr + 32 + length]
        keys += ["0x" + blob[i : i + PUBKEY_LEN].hex() for i in range(0, length, PUBKEY_LEN)]
    if len(keys) != deposited:
        raise RuntimeError(f"Nur {len(keys)} von {deposited} Keys gelesen")
    log.info("Execution Layer: %d deponierte Keys für Operator %d", len(keys), OPERATOR_ID)
    return keys


def fetch_pubkeys_kapi() -> list[str]:
    url = f"{KAPI_URL}/v1/modules/{LIDO_MODULE_ID}/keys?used=true&operatorIndex={OPERATOR_ID}"
    data = http_json(url, timeout=120)
    keys = [k["key"] for k in data["data"]["keys"] if k.get("used")]
    log.info("Keys API: %d genutzte Keys für Operator %d", len(keys), OPERATOR_ID)
    return keys


def fetch_operator_pubkeys() -> list[str]:
    if EXECUTION_URLS:
        try:
            return fetch_pubkeys_el()
        except Exception as e:  # noqa: BLE001
            if not KAPI_FALLBACK:
                raise
            log.error("Keine EL-Node erreichbar (%s), nutze Keys API", e)
    return fetch_pubkeys_kapi()


def resolve_validators(pubkeys: list[str]) -> dict[int, str]:
    """Liefert {validator_index: pubkey} für alle aktiven/pending Validatoren."""
    result: dict[int, str] = {}
    chunk = 500
    for i in range(0, len(pubkeys), chunk):
        part = pubkeys[i : i + chunk]
        try:
            resp = beacon("/eth/v1/beacon/states/head/validators", data={"ids": part})
        except urllib.error.HTTPError:
            # Fallback für Clients ohne POST-Support: GET in kleineren Blöcken
            resp = {"data": []}
            for j in range(0, len(part), 50):
                ids = ",".join(part[j : j + 50])
                resp["data"] += beacon(f"/eth/v1/beacon/states/head/validators?id={ids}")["data"]
        for v in resp["data"]:
            if v["status"].startswith(("active", "pending")):
                result[int(v["index"])] = v["validator"]["pubkey"]
    log.info("Beacon Node: %d aktive/pending Validatoren", len(result))
    return result


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            log.warning("state.json defekt, starte neu")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def new_daily() -> dict:
    return {"proposed": 0, "missed": 0, "critical": 0, "missed_list": []}


# --------------------------------------------------------------------------- #
# Monitor
# --------------------------------------------------------------------------- #
class Monitor:
    def __init__(self):
        self.state = load_state()
        self.validators: dict[int, str] = {}
        self.keys_loaded_at = 0.0
        self.duties: dict[int, dict[int, int] | None] = {}  # epoch -> {slot: proposer}
        self.error_since: float | None = None
        self.error_alerted = False
        self.state.setdefault("daily", new_daily())
        self.state.setdefault("streak", 0)  # verpasste Duties in Folge
        self.state.setdefault("recent", [])  # letzte Duties: 1 = Block, 0 = verpasst
        self.state.setdefault("val_misses", {})  # validator -> [slots]

    # -- Keys ---------------------------------------------------------------
    def refresh_keys(self, force: bool = False) -> None:
        if not force and time.time() - self.keys_loaded_at < KEYS_REFRESH_SEC:
            return
        try:
            vals = resolve_validators(fetch_operator_pubkeys())
            if not vals:
                raise RuntimeError("0 aktive Validatoren gefunden")
            self.validators = vals
            self.keys_loaded_at = time.time()
        except Exception as e:  # noqa: BLE001
            log.error("Konnte Keys nicht aktualisieren: %s", e)
            if not self.validators:
                raise

    # -- Beacon -------------------------------------------------------------
    def get_duties(self, epoch: int) -> dict[int, int] | None:
        """Proposer-Duties einer Epoch (gecacht). None = nicht verfügbar."""
        if epoch not in self.duties:
            try:
                resp = beacon(f"/eth/v1/validator/duties/proposer/{epoch}")
                self.duties[epoch] = {
                    int(d["slot"]): int(d["validator_index"]) for d in resp["data"]
                }
            except (urllib.error.HTTPError, NotFound) as e:
                # Manche Clients liefern nur aktuelle/nächste Epoch
                log.error("Duties für Epoch %d nicht verfügbar: %s", epoch, e)
                self.duties[epoch] = None
            for old in [e for e in self.duties if e < epoch - 8]:
                del self.duties[old]
        return self.duties[epoch]

    def head_slot(self) -> int:
        return int(beacon("/eth/v1/beacon/headers/head")["data"]["header"]["message"]["slot"])

    def block_exists(self, slot: int) -> bool:
        """Ein Slot gilt nur als verpasst, wenn KEIN erreichbarer Beacon Node
        einen kanonischen Block dafür kennt (schützt vor einem hinterherhinkenden
        Node)."""
        reachable = 0
        for base in [beacon_pool.urls[beacon_pool.current]] + [
            u for i, u in enumerate(beacon_pool.urls) if i != beacon_pool.current
        ]:
            try:
                http_json(f"{base}/eth/v1/beacon/headers/{slot}")
                return True
            except NotFound:
                reachable += 1
            except Exception as e:  # noqa: BLE001
                log.debug("Beacon %s nicht erreichbar: %s", base, e)
        if reachable == 0:
            raise RuntimeError("Kein Beacon Node erreichbar")
        return False

    # -- Schweregrad --------------------------------------------------------
    def record(self, ok: bool) -> None:
        recent = self.state["recent"]
        recent.append(1 if ok else 0)
        del recent[:-CRIT_WINDOW]

    def severity(self) -> tuple[str, str]:
        streak = self.state["streak"]
        window_misses = self.state["recent"].count(0)
        if streak >= CRIT_STREAK:
            return "critical", f"{streak} Duties in Folge verpasst"
        if CRIT_WINDOW_MISSES and window_misses >= CRIT_WINDOW_MISSES:
            return (
                "critical",
                f"{window_misses} der letzten {len(self.state['recent'])} Duties verpasst",
            )
        return "warning", "einzelner verpasster Block"

    def validator_misses(self, idx: int, slot: int) -> int:
        vm = self.state["val_misses"]
        slots = [s for s in vm.get(str(idx), []) if s > slot - 30 * SLOTS_PER_DAY]
        slots.append(slot)
        vm[str(idx)] = slots
        # Aufräumen
        for k in [k for k, v in vm.items() if not v or v[-1] < slot - 30 * SLOTS_PER_DAY]:
            del vm[k]
        return len(slots)

    # -- Kernlogik ----------------------------------------------------------
    def check_slot(self, slot: int) -> None:
        epoch = slot // SLOTS_PER_EPOCH
        duties = self.get_duties(epoch)
        if duties is None:
            return
        proposer = duties.get(slot)
        if proposer is None or proposer not in self.validators:
            return
        vlink = f'<a href="{EXPLORER}/validator/{proposer}">{proposer}</a>'
        slink = f'<a href="{EXPLORER}/slot/{slot}">{slot}</a>'

        if self.block_exists(slot):
            log.info("Slot %d: Block von Validator %d ✓", slot, proposer)
            prev_streak = self.state["streak"]
            self.state["streak"] = 0
            self.record(True)
            self.state["daily"]["proposed"] += 1
            if prev_streak >= CRIT_STREAK:
                telegram(
                    f"✅ <b>ENTWARNUNG</b> – Lido Operator {OPERATOR_ID}\n"
                    f"Wieder Block produziert nach {prev_streak} verpassten Duties.\n"
                    f"Slot {slink} · Validator {vlink}"
                )
            elif NOTIFY_PROPOSED:
                telegram(f"✅ Block vorgeschlagen · Slot {slink} · Validator {vlink}", silent=True)
            return

        self.state["streak"] += 1
        self.record(False)
        level, reason = self.severity()
        n_val = self.validator_misses(proposer, slot)
        d = self.state["daily"]
        d["missed"] += 1
        d["critical"] += level == "critical"
        d["missed_list"].append([slot, proposer, level])
        log.warning("Slot %d: KEIN Block von Validator %d [%s: %s]", slot, proposer, level, reason)

        if level == "critical":
            head = f"🔴 <b>KRITISCH: Block verpasst</b> – Lido Operator {OPERATOR_ID}"
        else:
            head = f"⚠️ <b>WARNUNG: Block verpasst</b> – Lido Operator {OPERATOR_ID}"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            head,
            f"<i>{reason}</i>",
            "",
            f"Validator: {vlink}",
            f"Slot: {slink} (Epoch {epoch})",
            f"Pubkey: <code>{self.validators[proposer]}</code>",
            f"Zeit: {ts}",
        ]
        if n_val > 1:
            lines.append(f"❗ Dieser Validator hat {n_val}× in 30 Tagen verpasst")
        telegram("\n".join(lines), silent=(level == "warning" and WARN_SILENT))

    def daily_summary(self) -> None:
        if DAILY_SUMMARY_HOUR_UTC == "":
            return
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour != int(DAILY_SUMMARY_HOUR_UTC) or self.state.get("summary_sent") == today:
            return
        d = self.state["daily"]
        total = d["proposed"] + d["missed"]
        rate = f"{100 * d['proposed'] / total:.2f}%" if total else "–"
        lines = [
            f"📊 <b>Tagesbericht</b> – Lido Operator {OPERATOR_ID}",
            f"Überwachte Validatoren: {len(self.validators)}",
            f"Blöcke vorgeschlagen: {d['proposed']}",
            f"Blöcke verpasst: {d['missed']} (davon kritisch: {d.get('critical', 0)})",
            f"Erfolgsquote: {rate}",
        ]
        if d["missed_list"]:
            lines.append("\nVerpasst:")
            for entry in d["missed_list"][-20:]:
                s, v = entry[0], entry[1]
                icon = "🔴" if len(entry) > 2 and entry[2] == "critical" else "⚠️"
                lines.append(f'{icon} Slot {s} – Validator <a href="{EXPLORER}/validator/{v}">{v}</a>')
        telegram("\n".join(lines), silent=True)
        self.state["summary_sent"] = today
        self.state["daily"] = new_daily()
        save_state(self.state)

    def tick(self) -> None:
        self.refresh_keys()
        head = self.head_slot()
        target = head - CONFIRM_SLOTS
        # Duties der aktuellen und nächsten Epoch vorab cachen, solange der
        # Beacon Node sie noch liefert
        head_epoch = head // SLOTS_PER_EPOCH
        self.get_duties(head_epoch)
        self.get_duties(head_epoch + 1)
        last = self.state.get("last_slot")
        if last is None:
            last = target - 1  # Beim allerersten Start nicht rückwirkend prüfen
        last = max(last, target - SLOTS_PER_DAY)  # max. ~1 Tag nachholen

        for slot in range(last + 1, target + 1):
            self.check_slot(slot)
            self.state["last_slot"] = slot
            if slot % SLOTS_PER_EPOCH == 0:
                save_state(self.state)
        save_state(self.state)
        self.daily_summary()

    def run(self) -> None:
        started = False
        while True:
            try:
                self.tick()
                if not started:
                    telegram(
                        f"🟢 Monitor gestartet – Lido Operator {OPERATOR_ID}\n"
                        f"{len(self.validators)} Validatoren · "
                        f"{len(BEACON_URLS)} Beacon / {len(EXECUTION_URLS)} EL Nodes",
                        silent=True,
                    )
                    started = True
                if self.error_alerted:
                    telegram("✅ Monitor läuft wieder normal.")
                self.error_since, self.error_alerted = None, False
            except Exception as e:  # noqa: BLE001
                log.error("Fehler: %s", e)
                self.error_since = self.error_since or time.time()
                if not self.error_alerted and time.time() - self.error_since > ALERT_AFTER_ERRORS_SEC:
                    telegram(
                        f"🟠 <b>Monitor-Fehler</b> seit {ALERT_AFTER_ERRORS_SEC // 60} min "
                        f"(Beacon/EL Nodes nicht erreichbar?)\n"
                        f"<code>{html.escape(str(e))[:300]}</code>"
                    )
                    self.error_alerted = True
            time.sleep(SECONDS_PER_SLOT)


def get_chat_id() -> None:
    """Zeigt alle Chats/Channels, in denen der Bot zuletzt Nachrichten gesehen hat."""
    if not TG_TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN fehlt")
    resp = http_json(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")
    chats = {}
    for upd in resp.get("result", []):
        for key in ("message", "channel_post", "my_chat_member", "edited_channel_post"):
            if key in upd:
                c = upd[key]["chat"]
                chats[c["id"]] = f"{c.get('type')}: {c.get('title') or c.get('username') or c.get('first_name')}"
    if not chats:
        print(
            "Keine Chats gefunden. Bot als Admin in den Channel aufnehmen, dort eine "
            "Nachricht posten und den Befehl erneut ausführen."
        )
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


def healthcheck() -> None:
    """Für Docker: gesund, wenn state.json in den letzten 5 Minuten geschrieben wurde."""
    ok = STATE_FILE.exists() and time.time() - STATE_FILE.stat().st_mtime < 300
    sys.exit(0 if ok else 1)


def el_diagnose(url: str) -> str:
    """Kurzer Zustandsbericht eines EL-Nodes (Netzwerk, Block, Sync)."""

    def rpc(method, params=None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        return http_json(url, data=payload, timeout=15).get("result")

    chain = int(rpc("eth_chainId"), 16)
    block = int(rpc("eth_blockNumber"), 16)
    syncing = rpc("eth_syncing")
    code = rpc("eth_getCode", [NOR_ADDRESS, "latest"]) or "0x"
    parts = [
        f"chainId={chain}" + ("" if chain == 1 else " (NICHT Mainnet!)"),
        f"Block={block}",
        "synchron" if syncing is False else f"SYNC LÄUFT ({syncing})",
        "Lido-Contract gefunden" if len(code) > 2 else "Lido-Contract NICHT gefunden",
    ]
    return ", ".join(parts)


def check_keys() -> None:
    """Vergleicht die Keys aller EL-Nodes mit der Lido Keys API."""
    kapi = set(fetch_pubkeys_kapi())
    print(f"Keys API: {len(kapi)} Keys")
    for url in EXECUTION_URLS:
        el_pool.urls, el_pool.current = [url], 0
        try:
            print(f"{url}: {el_diagnose(url)}")
        except Exception as e:  # noqa: BLE001
            print(f"{url}: Diagnose fehlgeschlagen: {e}")
        try:
            el = set(fetch_pubkeys_el())
            status = "OK" if el == kapi else f"ABWEICHUNG ({len(el ^ kapi)} Keys)"
            print(f"{url}: {len(el)} Keys – {status}")
        except Exception as e:  # noqa: BLE001
            print(f"{url}: FEHLER {e}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "test-telegram":
        telegram(f"🔔 Testnachricht vom Lido-Monitor (Operator {OPERATOR_ID})")
    elif cmd == "check-keys":
        check_keys()
    elif cmd == "get-chat-id":
        get_chat_id()
    elif cmd == "healthcheck":
        healthcheck()
    else:
        Monitor().run()
