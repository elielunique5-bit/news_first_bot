"""
Bot d'analyse des annonces économiques - Stratégie news-first (v4)
====================================================================
- Source de données : API officielle Finnhub (calendrier économique),
  à la place du feed gratuit non-officiel qui était sujet au blocage.
- Messages Telegram en tableaux réels (colonnes alignées, bloc <pre>).
- Fuseau horaire : tout est converti en heure de Kinshasa.
- Deux modes : RUN_MODE=briefing (récap quotidien, ~6h) ou
  RUN_MODE=watch (vérif publications fraîches, toutes les ~15 min).
- Anti-doublon : alertes individuelles + un seul briefing par jour.

Secrets requis dans le repo GitHub (Settings > Secrets and variables > Actions) :
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    FINNHUB_API_KEY

Variables optionnelles (même page, onglet "Variables") :
    SEUIL_ALERTE (def: 5)
    ENVOYER_MEME_SANS_ALERTE (def: true)
"""

import os
import json
import requests
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------

TZ = ZoneInfo("Africa/Kinshasa")

MARKETS = {
    "USD": "New York",
    "EUR": "Londres",
    "GBP": "Londres",
    "JPY": "Tokyo / Hong Kong",
    "CNY": "Hong Kong",
    "HKD": "Hong Kong",
    "AUD": "Sydney / Hong Kong",
    "CHF": "Londres / Zurich",
}

ASSET_MAP = {
    "USD": ["DXY", "XAUUSD", "indices US", "BTC/ETH"],
    "EUR": ["EURUSD", "DXY(inv)"],
    "GBP": ["GBPUSD", "GBPJPY"],
    "JPY": ["USDJPY", "GBPJPY", "XAUJPY"],
    "CNY": ["indices asia", "AUDUSD"],
    "HKD": ["indices HK", "USDHKD"],
    "AUD": ["AUDUSD", "AUDJPY"],
    "CHF": ["USDCHF", "XAUUSD"],
}

# Finnhub renvoie des codes pays ISO, pas des codes devise
COUNTRY_TO_CURRENCY = {
    "US": "USD", "EU": "EUR", "GB": "GBP", "JP": "JPY",
    "CN": "CNY", "HK": "HKD", "AU": "AUD", "CH": "CHF",
}

NIVEAU_1_KEYWORDS = [
    "interest rate", "rate decision", "fomc", "cpi", "core cpi",
    "non-farm", "nfp", "gdp", "press conference",
    "monetary policy statement", "boj", "ecb", "boe", "pboc", "rba",
]

NIVEAU_2_KEYWORDS = [
    "pmi", "retail sales", "jobless claims", "unemployment", "ppi",
    "trade balance", "industrial production", "consumer confidence",
    "speech", "speaks", "housing", "durable goods",
]

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"
STATE_FILE = "state/sent_events.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

RUN_MODE = os.environ.get("RUN_MODE", "briefing").strip().lower()

PUBLISH_WINDOW_MIN = 20
WATCH_HOURS = 36


def env_int(name, default):
    val = os.environ.get(name, "")
    if val is None or str(val).strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def env_bool(name, default):
    val = os.environ.get(name, "")
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() == "true"


SEUIL_ALERTE = env_int("SEUIL_ALERTE", 5)
ENVOYER_MEME_SANS_ALERTE = env_bool("ENVOYER_MEME_SANS_ALERTE", True)


# ---------------------------------------------------------------
# RÉCUPÉRATION (Finnhub) ET PARSING
# ---------------------------------------------------------------

def fetch_calendar():
    """Récupère le calendrier via l'API officielle Finnhub."""
    if not FINNHUB_API_KEY:
        print("FINNHUB_API_KEY manquant.")
        return []
    today = datetime.now(TZ).date()
    params = {
        "from": (today - timedelta(days=4)).isoformat(),
        "to": (today + timedelta(days=2)).isoformat(),
        "token": FINNHUB_API_KEY,
    }
    try:
        resp = requests.get(FINNHUB_URL, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json().get("economicCalendar", [])
    except Exception as e:
        print(f"Erreur de récupération du calendrier (Finnhub): {e}")
        return []


def classify_event(title):
    t = title.lower()
    if any(kw in t for kw in NIVEAU_1_KEYWORDS):
        return 1
    if any(kw in t for kw in NIVEAU_2_KEYWORDS):
        return 2
    return None


def parse_num(v):
    if v is None or v == "":
        return None
    s = str(v).strip().replace("%", "").replace(",", "")
    mult = 1
    if s.endswith("K"):
        mult, s = 1e3, s[:-1]
    elif s.endswith("M"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("B"):
        mult, s = 1e9, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def parse_events(raw_events):
    parsed = []
    for e in raw_events:
        impact = (e.get("impact") or "").lower()
        raw_country = e.get("country", "")
        currency = COUNTRY_TO_CURRENCY.get(raw_country, "")
        title = e.get("event", "")

        if impact != "high":
            continue
        if currency not in MARKETS:
            continue

        niveau = classify_event(title)
        if niveau is None:
            niveau = 2

        dt = None
        raw_time = e.get("time", "")
        try:
            dt_naive = datetime.strptime(raw_time, "%Y-%m-%d %H:%M:%S")
            dt = dt_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        except ValueError:
            dt = None

        parsed.append({
            "id": f"{currency}_{title}_{raw_time}",
            "titre": title,
            "devise": currency,
            "place": MARKETS.get(currency, "?"),
            "niveau": niveau,
            "datetime": dt,
            "actual": e.get("actual"),
            "forecast": e.get("estimate"),
            "previous": e.get("prev"),
        })
    return parsed


# ---------------------------------------------------------------
# SCÉNARIOS (neutres, pour les colonnes PREV/FCST + alerte publication)
# ---------------------------------------------------------------

def build_scenario_table(event):
    f, p = parse_num(event["forecast"]), parse_num(event["previous"])
    if f is None or p is None:
        return None
    ecart = f - p
    step = abs(ecart) if abs(ecart) > 1e-9 else max(abs(f) * 0.5, 0.1)
    trend_up = ecart >= 0
    if trend_up:
        forte, inverse = f + step * 0.5, p - step * 0.3
    else:
        forte, inverse = f - step * 0.5, p + step * 0.3
    return {"trend_up": trend_up, "consensus": f, "forte": forte, "inverse": inverse}


def classify_actual(event, table):
    a = parse_num(event["actual"])
    if a is None or table is None:
        return None
    f = table["consensus"]
    tol = max(abs(f) * 0.08, 0.05)
    if abs(a - f) <= tol:
        return "Conforme au consensus"
    if (table["trend_up"] and a > f) or (not table["trend_up"] and a < f):
        return "Confirmation forte"
    if (table["trend_up"] and a <= table["inverse"]) or (not table["trend_up"] and a >= table["inverse"]):
        return "Surprise inverse"
    return "Entre consensus et confirmation"


# ---------------------------------------------------------------
# ANALYSE GLOBALE (score de biais cumulé)
# ---------------------------------------------------------------

def determine_direction(event):
    a, f = parse_num(event.get("actual")), parse_num(event.get("forecast"))
    if a is None or f is None:
        return "attente"
    if a > f:
        return "au-dessus"
    elif a < f:
        return "en-dessous"
    return "conforme"


def build_bias_score(events, days_window=4):
    now = datetime.now(TZ)
    cutoff = now - timedelta(days=days_window)
    scores = defaultdict(int)
    details = defaultdict(list)
    for e in events:
        if e["datetime"] is None or not (cutoff <= e["datetime"] <= now):
            continue
        weight = 3 if e["niveau"] == 1 else 1
        direction = determine_direction(e)
        if direction == "au-dessus":
            scores[e["devise"]] += weight
        elif direction == "en-dessous":
            scores[e["devise"]] -= weight
        details[e["devise"]].append((e["titre"], direction, e["niveau"]))
    return scores, details


def upcoming_events(events, hours_ahead=WATCH_HOURS):
    now = datetime.now(TZ)
    limit = now + timedelta(hours=hours_ahead)
    up = [e for e in events if e["datetime"] and now <= e["datetime"] <= limit and not e.get("actual")]
    up.sort(key=lambda x: x["datetime"])
    return up


def just_published(events, state, window_min=PUBLISH_WINDOW_MIN):
    now = datetime.now(TZ)
    fresh = []
    for e in events:
        if not e.get("actual") or e["datetime"] is None:
            continue
        if e["id"] in state.get("alerted", []):
            continue
        delta_min = abs((now - e["datetime"]).total_seconds()) / 60
        if delta_min <= window_min or e["datetime"] <= now:
            fresh.append(e)
    return fresh


# ---------------------------------------------------------------
# ÉTAT
# ---------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"alerted": [], "last_briefing_date": ""}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["alerted"] = state.get("alerted", [])[-300:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------
# OUTILS TABLEAU (texte aligné, affiché dans <pre> sur Telegram)
# ---------------------------------------------------------------

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pad(val, width, right=False):
    s = str(val) if val not in (None, "") else "-"
    if len(s) > width:
        s = s[:width - 1] + "…"
    return s.rjust(width) if right else s.ljust(width)


def fmt_num(v):
    if v is None or v == "":
        return "-"
    return str(v)


def make_table(headers, rows, widths, rights=None):
    rights = rights or [False] * len(headers)
    header_line = " ".join(pad(h, w) for h, w in zip(headers, widths))
    sep = "-" * len(header_line)
    lines = [header_line, sep]
    for row in rows:
        lines.append(" ".join(pad(c, w, r) for c, w, r in zip(row, widths, rights)))
    return esc("\n".join(lines))


# ---------------------------------------------------------------
# MESSAGES TELEGRAM
# ---------------------------------------------------------------

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant. Message non envoyé:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i:i + 4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        })
        if resp.status_code != 200:
            print(f"Erreur envoi Telegram: {resp.status_code} - {resp.text}")


def build_publication_alert(event):
    table = build_scenario_table(event)
    resultat = classify_actual(event, table)

    rows = [
        ["Actual", fmt_num(event.get("actual"))],
        ["Forecast", fmt_num(event.get("forecast"))],
        ["Previous", fmt_num(event.get("previous"))],
    ]
    if table:
        rows.append(["Conforme", f"~{table['consensus']:.2f}"])
        rows.append(["Confirm. forte", f"{'>' if table['trend_up'] else '<'}{table['forte']:.2f}"])
        rows.append(["Surprise inv.", f"{'<' if table['trend_up'] else '>'}{table['inverse']:.2f}"])

    tbl = make_table(["CHAMP", "VALEUR"], rows, [15, 12], [False, True])

    lines = [f"🔔 <b>PUBLICATION — {esc(event['titre'])} ({event['devise']})</b>"]
    lines.append(f"{event['datetime'].strftime('%a %d/%m %Hh%M')} (Kinshasa)")
    lines.append(f"<pre>{tbl}</pre>")
    if resultat:
        lines.append(f"<b>→ {esc(resultat)}</b>")
    lines.append(f"<b>Actifs :</b> {esc(', '.join(ASSET_MAP.get(event['devise'], ['-'])))}")
    return "\n".join(lines)


def build_daily_briefing(events):
    scores, details = build_bias_score(events)
    fortes = {d: s for d, s in scores.items() if abs(s) >= SEUIL_ALERTE}
    faibles = {d: s for d, s in scores.items() if abs(s) < SEUIL_ALERTE}

    lines = []

    if fortes:
        rows = []
        for devise, score in sorted(fortes.items(), key=lambda x: -abs(x[1])):
            tendance = "HAUSSIER" if score > 0 else "BAISSIER"
            rows.append([devise, f"{score:+d}", tendance])
        tbl = make_table(["DEV", "SCORE", "TENDANCE"], rows, [4, 6, 9], [False, True, False])
        lines.append(f"🔴 <b>BIAIS FORT (seuil {SEUIL_ALERTE})</b>")
        lines.append(f"<pre>{tbl}</pre>")
        for devise in fortes:
            actifs = ", ".join(ASSET_MAP.get(devise, ["-"]))
            lines.append(f"<b>{devise}</b> → {esc(actifs)}")
    else:
        lines.append(f"<b>Aucun biais au-delà du seuil ({SEUIL_ALERTE}) sur 4 jours.</b>")
        lines.append("Patience — pas de conviction suffisante.")

    if faibles:
        rows = []
        for devise, score in sorted(faibles.items(), key=lambda x: -abs(x[1])):
            tendance = "haussier" if score > 0 else "baissier" if score < 0 else "neutre"
            rows.append([devise, f"{score:+d}", tendance])
        tbl = make_table(["DEV", "SCORE", "TEND."], rows, [4, 6, 9], [False, True, False])
        lines.append("\n<b>Sous le seuil (à surveiller)</b>")
        lines.append(f"<pre>{tbl}</pre>")

    lines.append(f"\n<b>À VENIR — {WATCH_HOURS}h (Kinshasa)</b>")
    up = upcoming_events(events)
    if not up:
        lines.append("Rien de prévu, pas encore publié.")
    else:
        rows = []
        devises_vues = []
        for e in up:
            date_str = e["datetime"].strftime("%d/%m %Hh%M")
            rows.append([
                f"N{e['niveau']}", e["devise"], e["titre"],
                date_str, fmt_num(e.get("previous")), fmt_num(e.get("forecast")),
            ])
            if e["devise"] not in devises_vues:
                devises_vues.append(e["devise"])
        tbl = make_table(
            ["N", "DEV", "ÉVÉNEMENT", "QUAND", "PREV", "FCST"],
            rows, [2, 4, 18, 11, 7, 7], [False, False, False, False, True, True],
        )
        lines.append(f"<pre>{tbl}</pre>")
        lines.append("<b>Actifs à surveiller :</b>")
        for devise in devises_vues:
            actifs = ", ".join(ASSET_MAP.get(devise, ["-"]))
            lines.append(f"  {devise} → {esc(actifs)}")

    return "\n".join(lines)


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------

def main():
    print(f"Mode d'exécution : {RUN_MODE}")
    print("Récupération du calendrier économique (Finnhub)...")
    raw = fetch_calendar()
    if not raw:
        if RUN_MODE == "briefing":
            send_telegram("Bot news-first : impossible de récupérer le calendrier aujourd'hui (Finnhub).")
        return

    events = parse_events(raw)
    state = load_state()

    fresh = just_published(events, state)
    for e in fresh:
        msg = build_publication_alert(e)
        print(msg)
        send_telegram(msg)
        state.setdefault("alerted", []).append(e["id"])

    if RUN_MODE == "briefing":
        today_str = datetime.now(TZ).strftime("%Y-%m-%d")
        if state.get("last_briefing_date") == today_str:
            print("Briefing déjà envoyé aujourd'hui.")
        else:
            briefing = build_daily_briefing(events)
            print(briefing)
            if ENVOYER_MEME_SANS_ALERTE or "🔴" in briefing:
                send_telegram(briefing)
            state["last_briefing_date"] = today_str
    else:
        print("Mode watch : pas de briefing complet.")

    save_state(state)


if __name__ == "__main__":
    main()
