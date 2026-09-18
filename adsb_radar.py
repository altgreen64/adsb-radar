#!/usr/bin/env python3
"""
ADS-B Radar — live terminal radar z pozycjami samolotów
Przechwytuje 1090 MHz i rysuje samoloty na radarze ASCII
"""

import os
import sys
import time
import math
import threading
import subprocess
from collections import defaultdict

import pyModeS as pms
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich import box

# ─── Lokalizacja domyślna (środek Polski — nadpisz jeśli znasz swoją) ─────────
HOME_LAT = 52.0
HOME_LON = 19.0

RADAR_W  = 71   # szerokość siatki (nieparzysta)
RADAR_H  = 35   # wysokość siatki (nieparzysta)
RADAR_KM = 300  # promień radaru w km

# ─── Dane globalne (chronione lockiem) ────────────────────────────────────────
lock    = threading.Lock()
flights: dict[str, dict] = {}
oe_buf: dict[str, dict]  = {}   # CPR odd/even bufory


def km_to_deg_lat(km):
    return km / 111.0

def km_to_deg_lon(km, lat):
    return km / (111.0 * math.cos(math.radians(lat)))

def latlon_to_radar(lat, lon):
    """Przelicza lat/lon na (col, row) na siatce radaru."""
    dlat = lat - HOME_LAT
    dlon = lon - HOME_LON
    cy = RADAR_H // 2
    cx = RADAR_W // 2
    # skalowanie: pełny promień = RADAR_KM km
    scale_lat = (RADAR_H / 2) / RADAR_KM * 111.0
    scale_lon = (RADAR_W / 2) / RADAR_KM * (111.0 * math.cos(math.radians(HOME_LAT)))
    row = int(cy - dlat * scale_lat)
    col = int(cx + dlon * scale_lon)
    return col, row

def draw_radar(flights_snap: dict) -> Text:
    """Rysuje siatkę radaru i nakłada samoloty."""
    cx = RADAR_W // 2
    cy = RADAR_H // 2

    # Inicjuj pustą siatkę
    grid = [[" "] * RADAR_W for _ in range(RADAR_H)]
    colors = [[None] * RADAR_W for _ in range(RADAR_H)]

    # Pierścienie radaru (50%, 100% promienia)
    for pct in [0.33, 0.66, 1.0]:
        ry = int(cy * pct)
        rx = int(cx * pct)
        for angle in range(0, 360, 2):
            rad = math.radians(angle)
            r = int(cy * pct)
            row = cy - int(r * math.cos(rad) * (cy / cx))
            col = cx + int(r * math.sin(rad))
            if 0 <= row < RADAR_H and 0 <= col < RADAR_W:
                if grid[row][col] == " ":
                    grid[row][col] = "·"
                    colors[row][col] = "dim"

    # Osie
    for r in range(RADAR_H):
        if grid[r][cx] in (" ", "·"):
            grid[r][cx] = "│"
            colors[r][cx] = "dim"
    for c in range(RADAR_W):
        if grid[cy][c] in (" ", "·"):
            grid[cy][c] = "─"
            colors[cy][c] = "dim"
    grid[cy][cx] = "+"

    # Etykiety pierścieni
    km1 = int(RADAR_KM * 0.33)
    km2 = int(RADAR_KM * 0.66)
    for km, pct in [(km1, 0.33), (km2, 0.66), (RADAR_KM, 1.0)]:
        lbl = f"{km}km"
        col = cx + int(cx * pct) + 1
        row = cy
        for i, ch in enumerate(lbl):
            if 0 <= col+i < RADAR_W:
                grid[row][col+i] = ch
                colors[row][col+i] = "dim"

    # Samoloty
    plane_labels = []
    with_pos = 0
    for icao, d in flights_snap.items():
        lat, lon = d.get("lat"), d.get("lon")
        alt = d.get("alt")
        cs  = (d.get("callsign") or icao[:6]).ljust(6)[:6]

        if lat is not None and lon is not None:
            col, row = latlon_to_radar(lat, lon)
            if 0 <= row < RADAR_H and 0 <= col < RADAR_W:
                sym = "▲"
                color = "red" if (alt and alt < 3000) else "bright_green"
                grid[row][col] = sym
                colors[row][col] = color
                # Callsign obok
                for i, ch in enumerate(cs.strip()):
                    nc = col + 1 + i
                    if 0 <= nc < RADAR_W and grid[row][nc] == " ":
                        grid[row][nc] = ch
                        colors[row][nc] = color
                with_pos += 1
                plane_labels.append((icao, cs.strip(), lat, lon, alt, color))
        else:
            # Bez pozycji — poza radarywm, będą w bocznej liście
            plane_labels.append((icao, cs.strip(), None, None, alt, "yellow"))

    # Środek = Ty
    grid[cy][cx] = "◉"
    colors[cy][cx] = "cyan"

    # Zbuduj Rich Text
    txt = Text()
    for r in range(RADAR_H):
        for c in range(RADAR_W):
            ch = grid[r][c]
            col_name = colors[r][c]
            if col_name:
                txt.append(ch, style=col_name)
            else:
                txt.append(ch)
        txt.append("\n")

    return txt, plane_labels, with_pos


def make_sidebar(plane_labels: list, total_msgs: int) -> Table:
    table = Table(box=box.SIMPLE, show_header=True, padding=(0, 1),
                  title=f"[dim]msg łącznie: {total_msgs}[/dim]")
    table.add_column("Lot",    style="bold white", width=8)
    table.add_column("Alt",    style="yellow",     width=8, justify="right")
    table.add_column("Pozycja",style="dim",        width=14)
    for icao, cs, lat, lon, alt, color in sorted(plane_labels,
            key=lambda x: -(x[4] or 0) if x[4] else 0):
        alt_str = f"{alt:,}ft" if alt else "—"
        if lat is not None:
            pos_str = f"{lat:.2f},{lon:.2f}"
        else:
            pos_str = "[dim]brak GPS[/dim]"
        table.add_row(f"[{color}]{cs}[/{color}]", alt_str, pos_str)
    return table


def capture_loop(proc: subprocess.Popen):
    """Wątek przechwytujący i dekodujący ramki ADS-B."""
    global flights, oe_buf
    for line in proc.stdout:
        line = line.strip().lstrip("*").rstrip(";")
        if len(line) < 14:
            continue
        try:
            msg = pms.decode(line)
            if not msg or msg.get("df") not in (17, 18):
                continue
            icao = msg.get("icao")
            if not icao:
                continue
            with lock:
                if icao not in flights:
                    flights[icao] = {"icao": icao, "callsign": None,
                                     "alt": None, "lat": None, "lon": None,
                                     "msgs": 0, "last_seen": 0}
                d = flights[icao]
                d["msgs"]     += 1
                d["last_seen"] = time.time()
                cs = msg.get("callsign")
                if cs and "#" not in str(cs):
                    d["callsign"] = str(cs).strip()
                if msg.get("altitude") is not None:
                    d["alt"] = msg["altitude"]

                # CPR pozycja
                tc = msg.get("typecode") or msg.get("tc")
                oe = msg.get("oe_flag")
                if tc and 9 <= int(tc) <= 18 and oe is not None:
                    if icao not in oe_buf:
                        oe_buf[icao] = {}
                    oe_buf[icao][int(oe)] = (line, time.time())
                    buf = oe_buf[icao]
                    if 0 in buf and 1 in buf:
                        t0 = buf[0][1]; t1 = buf[1][1]
                        if abs(t0 - t1) < 15:   # maks 15s między ramkami
                            try:
                                pos = pms.adsb.position(buf[0][0], buf[1][0], t0, t1)
                                if pos and -90 <= pos[0] <= 90 and -180 <= pos[1] <= 180:
                                    d["lat"], d["lon"] = round(pos[0], 4), round(pos[1], 4)
                            except Exception:
                                pass
        except Exception:
            pass


def main():
    global HOME_LAT, HOME_LON, RADAR_KM
    import argparse
    parser = argparse.ArgumentParser(description="ADS-B Terminal Radar")
    parser.add_argument("--time",    type=int, default=0,
                        help="Czas działania w sekundach (0 = nieskończony)")
    parser.add_argument("--lat",     type=float, default=HOME_LAT,
                        help=f"Twoja szerokość geogr. (domyślnie {HOME_LAT})")
    parser.add_argument("--lon",     type=float, default=HOME_LON,
                        help=f"Twoja długość geogr. (domyślnie {HOME_LON})")
    parser.add_argument("--radius",  type=int, default=RADAR_KM,
                        help=f"Promień radaru w km (domyślnie {RADAR_KM})")
    args = parser.parse_args()

    HOME_LAT  = args.lat
    HOME_LON  = args.lon
    RADAR_KM  = args.radius

    console = Console()
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]ADS-B RADAR[/bold cyan]  [dim]1090 MHz live[/dim]\n"
        f"[dim]Centrum: {HOME_LAT}°N {HOME_LON}°E  |  Zasięg: ±{RADAR_KM} km[/dim]",
        border_style="cyan", padding=(0, 3),
    ))
    console.print("[dim]Ctrl+C aby zakończyć[/dim]\n")

    try:
        proc = subprocess.Popen(
            ["rtl_adsb", "-g", "49"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        console.print("[red]rtl_adsb nie znaleziony[/red]")
        sys.exit(1)

    t = threading.Thread(target=capture_loop, args=(proc,), daemon=True)
    t.start()

    start = time.time()
    sweep_angle = 0
    total_msgs  = 0

    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            while True:
                sweep_angle = (sweep_angle + 15) % 360
                with lock:
                    # Usuń samoloty niewidziane >60s
                    now = time.time()
                    stale = [k for k, v in flights.items()
                             if now - v["last_seen"] > 60]
                    for k in stale:
                        del flights[k]
                    snap = dict(flights)
                    total_msgs = sum(d["msgs"] for d in snap.values())

                radar_txt, plane_labels, with_pos = draw_radar(snap)
                sidebar = make_sidebar(plane_labels, total_msgs)

                elapsed = int(time.time() - start)
                header = (
                    f"[cyan]● LIVE[/cyan]  "
                    f"[dim]{elapsed}s[/dim]  "
                    f"transponderów: [bold]{len(snap)}[/bold]  "
                    f"z pozycją GPS: [green]{with_pos}[/green]  "
                    f"[dim]{time.strftime('%H:%M:%S')}[/dim]"
                )

                radar_panel = Panel(
                    radar_txt,
                    title=header,
                    border_style="green",
                    padding=(0, 1),
                )
                sidebar_panel = Panel(
                    sidebar,
                    title="[bright_blue]Loty[/bright_blue]",
                    border_style="bright_blue",
                    padding=(0, 0),
                )

                live.update(Columns([radar_panel, sidebar_panel], equal=False))

                if args.time and elapsed >= args.time:
                    break
                time.sleep(0.25)

    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait(timeout=3)
        console.print(f"\n[dim]Sesja zakończona. Przechwycono {len(flights)} transponderów.[/dim]")


if __name__ == "__main__":
    main()
