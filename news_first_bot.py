"""
Bot d'analyse des annonces économiques - Stratégie news-first
================================================================
Version GitHub Actions : tourne sur un cron, envoie le briefing
directement sur Telegram (pas d'exécution locale nécessaire).

Secrets requis dans le repo GitHub (Settings > Secrets and variables > Actions):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Source de données:
    Feed JSON public de Forex Factory (non officiel, peut changer).
"""

import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# ---------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------

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
    "USD": ["DXY", "XAUUSD", "indices US", "BTC/ETH (indirect)"],
    "EUR": ["EURUSD", "DXY (inverse)"],
    "GBP": ["GBPUSD", "GBPJPY"],
    "JPY": ["USDJPY", "GBPJPY", "XAUJPY"],
    "CNY": ["indices asiatiques", "AUDUSD (proxy Chine)"],
    "HKD": ["indices Hong Kong", "USDHKD"],
    "AUD": ["AUDUSD", "AUDJPY"],
    "CHF": ["USDCHF", "XAUUSD (refuge)"],
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

FOREX_FACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Seuil à partir duquel un biais est jugé assez fort pour alerter.
# Rappel des poids: Niveau 1 = 3, Niveau 2 = 1.
# SEUIL = 5 veut dire par ex: 2 événements niveau 1 alignés (3+3=6) déclenchent
# une alerte, mais 1 seul niveau 1 (score 3) ou des niveau 2 isolés ne suffisent pas.
SEUIL_ALERTE = int(os.environ.get("SEUIL_ALERTE", "5"))


# ---------------------------------------------------------------
# RÉCUPÉRATION ET CLASSIFICATION
# ---------------------------------------------------------------

def fetch_calendar():
    try:
        resp = requests.get(FOREX_FACTORY_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Erreur de récupération du calendrier: {e}")
        return []


def classify_event(title):
    t = title.lower()
    if any(kw in t for kw in NIVEAU_1_KEYWORDS):
        return 1
    if any(kw in t for kw in NIVEAU_2_KEYWORDS):
        return 2
    return None


def parse_events(raw_events):
    parsed = []
    for e in raw_events:
        impact = (e.get("impact") or "").lower()
        currency = e.get("country", "")
        title = e.get("title", "")

        if impact != "high":
            continue
        if currency not in MARKETS:
            continue

        niveau = classify_event(title)
        if niveau is None:
            niveau = 2

        dt = None
        raw_date = e.get("date", "")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(raw_date, fmt)
                break
            except ValueError:
                continue

        parsed.append({
            "titre": title,
            "devise": currency,
            "place": MARKETS.get(currency, "?"),
            "niveau": niveau,
            "datetime": dt,
            "actual": e.get("actual"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
        })
    return parsed


# ---------------------------------------------------------------
# ANALYSE
# ---------------------------------------------------------------

def determine_direction(event):
    actual, forecast = event.get("actual"), event.get("forecast")
    if not actual or not forecast:
        return "en attente de publication"

    def clean(v):
        return float(str(v).replace("%", "").replace("K", "").replace(",", "").strip())

    try:
        a, f = clean(actual), clean(forecast)
    except ValueError:
        return "en attente de publication"

    if a > f:
        return "au-dessus des attentes"
    elif a < f:
        return "en-dessous des attentes"
    return "conforme aux attentes"


def build_bias_score(events, days_window=4):
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=days_window)

    scores = defaultdict(int)
    details = defaultdict(list)

    for e in events:
        if e["datetime"] is None or not (cutoff <= e["datetime"] <= now):
            continue

        weight = 3 if e["niveau"] == 1 else 1
        direction = determine_direction(e)

        if direction == "au-dessus des attentes":
            scores[e["devise"]] += weight
        elif direction == "en-dessous des attentes":
            scores[e["devise"]] -= weight

        details[e["devise"]].append((e["titre"], direction, e["niveau"]))

    return scores, details


def upcoming_events(events, hours_ahead=36):
    now = datetime.now().astimezone()
    limit = now + timedelta(hours=hours_ahead)
    up = [e for e in events if e["datetime"] and now <= e["datetime"] <= limit]
    up.sort(key=lambda x: x["datetime"])
    return up


# ---------------------------------------------------------------
# CONSTRUCTION DU MESSAGE
# ---------------------------------------------------------------

def build_message(events, seuil=SEUIL_ALERTE):
    """
    Construit le message. Sépare les devises qui dépassent le seuil
    (biais fort, alerte prioritaire) de celles en dessous (info seulement).
    Retourne (message, alerte_declenchee: bool)
    """
    scores, details = build_bias_score(events)

    fortes = {d: s for d, s in scores.items() if abs(s) >= seuil}
    faibles = {d: s for d, s in scores.items() if abs(s) < seuil}

    lines = []
    alerte_declenchee = len(fortes) > 0

    if fortes:
        lines.append(f"<b>🔴 BIAIS FORT (seuil {seuil} dépassé)</b>")
        for devise, score in sorted(fortes.items(), key=lambda x: -abs(x[1])):
            tendance = "HAUSSIER" if score > 0 else "BAISSIER"
            lines.append(f"\n<b>{devise}</b> ({MARKETS.get(devise)}) — score {score:+d} → {tendance} CONFIRMÉ")
            lines.append(f"Actifs: {', '.join(ASSET_MAP.get(devise, ['-']))}")
            for titre, direction, niveau in details[devise]:
                lines.append(f"  [N{niveau}] {titre} → {direction}")
    else:
        lines.append(f"<b>Aucun biais n'a dépassé le seuil ({seuil}) aujourd'hui.</b>")
        lines.append("Pas de conviction suffisante pour trader sur base du narratif — patience.")

    if faibles:
        lines.append(f"\n<b>Biais sous le seuil (à surveiller, pas encore exploitable)</b>")
        for devise, score in sorted(faibles.items(), key=lambda x: -abs(x[1])):
            tendance = "haussier" if score > 0 else "baissier" if score < 0 else "neutre"
            lines.append(f"  {devise}: {score:+d} ({tendance})")

    lines.append("\n<b>À VENIR — prochaines 36h</b>")
    up = upcoming_events(events)
    if not up:
        lines.append("Rien de prévu à fort impact.")
    else:
        for e in up:
            date_str = e["datetime"].strftime("%a %d/%m %Hh%M") if e["datetime"] else "?"
            marqueur = " ⚠️ devise en biais fort" if e["devise"] in fortes else ""
            lines.append(f"\n[N{e['niveau']}] {e['titre']} ({e['devise']} - {e['place']}){marqueur}")
            lines.append(f"Prévu: {date_str} | Forecast: {e.get('forecast', 'n/a')} | Previous: {e.get('previous', 'n/a')}")

    return "\n".join(lines), alerte_declenchee


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant. Message non envoyé:")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Telegram limite à 4096 caractères par message -> on découpe si besoin
    chunks = [message[i:i + 4000] for i in range(0, len(message), 4000)]

    for chunk in chunks:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        })
        if resp.status_code != 200:
            print(f"Erreur envoi Telegram: {resp.status_code} - {resp.text}")


def main():
    print("Récupération du calendrier économique...")
    raw = fetch_calendar()
    if not raw:
        send_telegram("Bot news-first: impossible de récupérer le calendrier économique aujourd'hui.")
        return

    events = parse_events(raw)
    message, alerte = build_message(events)
    print(message)

    # ENVOYER_MEME_SANS_ALERTE=false -> n'envoie sur Telegram que si un seuil
    # est dépassé quelque part. Par défaut on envoie toujours le briefing
    # (utile pour garder l'habitude de lire le calendrier), mais tu peux
    # passer cette variable d'env à "false" dans le workflow si tu préfères
    # être notifié uniquement quand ça compte.
    envoyer_toujours = os.environ.get("ENVOYER_MEME_SANS_ALERTE", "true").lower() == "true"

    if alerte or envoyer_toujours:
        send_telegram(message)
    else:
        print("Aucun biais fort détecté — message non envoyé sur Telegram (ENVOYER_MEME_SANS_ALERTE=false).")


if __name__ == "__main__":
    main()
