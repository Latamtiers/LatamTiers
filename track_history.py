#!/usr/bin/env python3
"""
track_history.py — Corre dentro de GitHub Actions cada X minutos.

1. Descarga el ranking actual desde Lunaris
2. Actualiza history.json (añade eventos cuando alguien cambia de tier)
3. Guarda snapshot en ranking.json (para que el HTML lo lea directo)

Todo queda commiteado por el workflow.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
LUNARIS_HOST = "mx.lunarishost.com"
LUNARIS_PORT = 20037
LUNARIS_PATH = "/web"
LUNARIS_URL = f"http://{LUNARIS_HOST}:{LUNARIS_PORT}{LUNARIS_PATH}"
TIMEOUT = 20

BASE_DIR = Path(__file__).parent
HISTORY_PATH = BASE_DIR / "history.json"
RANKING_PATH = BASE_DIR / "ranking.json"

TIER_SCORE = {
    "HT1": 60, "LT1": 45,
    "HT2": 30, "LT2": 20,
    "HT3": 10, "LT3": 6,
    "HT4": 4,  "LT4": 3,
    "HT5": 2,  "LT5": 1,
}

TIERS_LOW_TO_HIGH = [
    "LT5", "HT5", "LT4", "HT4", "LT3", "HT3",
    "LT2", "HT2", "LT1", "HT1",
]

MODE_ALIASES = {
    "SWORD": "sword", "AXE": "axe", "MACE": "mace", "POT": "pot",
    "NETHPOT": "nethpot", "NETHERITE_POT": "nethpot", "NETHER_POT": "nethpot",
    "UHC": "uhc", "SHIELDLESS_UHC": "uhc",
    "SMP": "smp",
    "CRYSTAL": "crystal", "DIAMOND_SMP": "crystal", "DIASMP": "crystal",
}
VALID_MODES = set(MODE_ALIASES.values())


# ============================================================
# DNS + FETCH
# ============================================================
def resolve_host(hostname: str) -> str | None:
    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        return infos[0][4][0] if infos else None
    except socket.gaierror:
        return None


def fetch_lunaris() -> dict:
    ip = resolve_host(LUNARIS_HOST)
    if ip is None:
        raise RuntimeError(f"No se pudo resolver {LUNARIS_HOST}")

    headers = {"Accept": "application/json", "User-Agent": "PVPHQ-gha/1.0"}

    # Intento 1: hostname
    try:
        req = urllib.request.Request(LUNARIS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except (socket.gaierror, urllib.error.URLError):
        pass

    # Intento 2: IP directa
    url_ip = f"http://{ip}:{LUNARIS_PORT}{LUNARIS_PATH}"
    headers_ip = dict(headers)
    headers_ip["Host"] = f"{LUNARIS_HOST}:{LUNARIS_PORT}"
    req = urllib.request.Request(url_ip, headers=headers_ip)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


# ============================================================
# HELPERS
# ============================================================
def now_ms() -> int:
    return int(time.time() * 1000)


def iso_to_ms(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def normalize_mode(raw_key: str) -> str | None:
    key = raw_key.strip().upper()
    mode = MODE_ALIASES.get(key, raw_key.strip().lower())
    return mode if mode in VALID_MODES else None


def extract_modes(player: dict) -> dict[str, dict]:
    result = {}
    tiers = player.get("tiers") or player.get("modalidades") or {}
    for raw_key, val in tiers.items():
        if not isinstance(val, dict):
            continue
        mode = normalize_mode(raw_key)
        if mode is None:
            continue
        tier = val.get("tier")
        if not isinstance(tier, str) or tier not in TIER_SCORE:
            continue
        at = iso_to_ms(val.get("obtained_at")) or val.get("updatedAt") or now_ms()
        result[mode] = {"tier": tier, "at": at}
    return result


def compute_direction(prev, curr):
    if prev is None:
        return "up"
    if prev not in TIERS_LOW_TO_HIGH or curr not in TIERS_LOW_TO_HIGH:
        return "same"
    pi = TIERS_LOW_TO_HIGH.index(prev)
    ci = TIERS_LOW_TO_HIGH.index(curr)
    return "up" if ci > pi else "down" if ci < pi else "same"


def atomic_write_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ============================================================
# TRACKING
# ============================================================
def main() -> int:
    print(f"📡 Descargando de {LUNARIS_URL}")
    try:
        ranking = fetch_lunaris()
    except Exception as e:
        print(f"❌ Error al descargar: {e}", file=sys.stderr)
        return 1

    print(f"   → {len(ranking)} jugadores")

    # Guarda snapshot
    atomic_write_json(RANKING_PATH, ranking)
    print(f"💾 Snapshot guardado en {RANKING_PATH.name}")

    # Carga historial
    if HISTORY_PATH.exists():
        try:
            with HISTORY_PATH.open("r", encoding="utf-8") as f:
                history = json.load(f)
        except json.JSONDecodeError:
            history = {}
    else:
        history = {}
        print("🌱 Primera ejecución — creando historial")

    cambios = []
    for uuid, player in ranking.items():
        if not isinstance(player, dict):
            continue
        nick = player.get("nick", "?")
        region = player.get("region")
        modes = extract_modes(player)

        if uuid not in history:
            history[uuid] = {"nick": nick, "modes": {}}
        history[uuid]["nick"] = nick
        if region:
            history[uuid]["region"] = region
        hmodes = history[uuid].setdefault("modes", {})

        for mode, info in modes.items():
            cur_tier, cur_at = info["tier"], info["at"]
            entries = hmodes.get(mode, [])
            if not entries:
                hmodes[mode] = [{"tier": cur_tier, "at": cur_at}]
                cambios.append((nick, mode, None, cur_tier, "up", True))
                continue
            last = entries[-1]
            if last.get("tier") != cur_tier:
                use_at = cur_at if cur_at > last.get("at", 0) else now_ms()
                entries.append({"tier": cur_tier, "at": use_at})
                d = compute_direction(last.get("tier"), cur_tier)
                cambios.append((nick, mode, last.get("tier"), cur_tier, d, False))

    history["__meta__"] = {
        "lastUpdate": now_ms(),
        "lastUpdateHuman": datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": LUNARIS_URL,
        "version": 3,
    }
    atomic_write_json(HISTORY_PATH, history)
    print(f"💾 Historial guardado en {HISTORY_PATH.name}")

    seeded = [c for c in cambios if c[5]]
    real = [c for c in cambios if not c[5]]

    if seeded:
        print(f"🌱 {len(seeded)} modos nuevos")
    if real:
        print(f"✨ {len(real)} cambio(s):")
        for nick, mode, frm, to, d, _ in real[:30]:
            arrow = "↑" if d == "up" else "↓" if d == "down" else "="
            print(f"   {arrow} {nick:20s} {mode:8s}  {frm or 'UNRANKED':8s} → {to}")
    elif not seeded:
        print("💤 Sin cambios")

    return 0


if __name__ == "__main__":
    sys.exit(main())