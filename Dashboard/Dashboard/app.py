"""
ReCarga — Dashboard de simulação de recarga de carros elétricos

Inclui simulação de geração solar fotovoltaica (energia renovável) integrada
ao motor de gerenciamento de demanda: a energia solar gerada em tempo real
aumenta a capacidade disponível para os carregadores pós-pago, reduzindo a
dependência da rede elétrica e os eventos de sobrecarga.
"""

import json
import math
import random
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuração / simulação
# ---------------------------------------------------------------------------
VOLTAGE = 230
GRID_CAPACITY_A = 40          # capacidade contratada junto à concessionária (rede)
PRICE_PRE = 1.35
PRICE_POS = 0.98
TICK_SECONDS = 1.5
TICK_HOURS = TICK_SECONDS / 3600
CAR_NAMES = ["Onix EV", "HB20 e", "Kwid Volt", "Compass e", "Fastback e", "Corolla e+"]
PORT = 5000

# --- Energia renovável (geração solar fotovoltaica simulada) ---------------
SOLAR_INSTALLED_KWP = 30          # potência instalada dos painéis solares na cobertura do estacionamento
SOLAR_BONUS_CAP_RATIO = 0.5       # bônus de capacidade solar limitado a 50% da capacidade da rede (segurança do barramento)
SOLAR_DAY_TICKS = 240             # nº de ciclos (step()) para simular um "dia" completo — acelerado para fins de demonstração
GRID_EMISSION_FACTOR_KG_KWH = 0.117  # fator médio de emissão da rede elétrica (kg CO2/kWh) — valor de referência simplificado, uso didático


def solar_output_factor(tick: int) -> float:
    """Curva de geração solar ao longo do 'dia' simulado (0 à noite, pico ao meio-dia)."""
    hour = (tick % SOLAR_DAY_TICKS) / SOLAR_DAY_TICKS * 24
    if hour < 6 or hour > 18:
        return 0.0, hour
    raw = math.sin(math.pi * (hour - 6) / 12)          # curva senoidal 6h–18h, pico às 12h
    cloud_factor = random.uniform(0.82, 1.0)             # variação simulando nebulosidade
    return max(0.0, raw) * cloud_factor, hour

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

TEMPLATES_DIR = PROJECT_DIR / "template"
STATIC_DIR = PROJECT_DIR / "Static"

lock = threading.Lock()


def make_station(i, plan, active):
    return dict(
        id=i, name=CAR_NAMES[(i - 1) % len(CAR_NAMES)], plan=plan, active=active,
        target_a=16 if plan == "pre" else 10, current_a=0.0,
        status="carregando" if active else "livre", energy_kwh=0.0, cost=0.0,
    )


state = {
    "stations": [
        make_station(1, "pre", True), make_station(2, "pos", True),
        make_station(3, "pos", True), make_station(4, "pre", True),
        make_station(5, "pos", False), make_station(6, "pre", False),
    ],
    "history": [dict(t=0, pre=0.0, pos=0.0, load=0.0, solar_kw=0.0, grid_kw=0.0)],
    "overload_events": 0,
    "overload_flag": False,
    "clock_tick": 60,  # inicia a simulação por volta das 6h (nascer do sol), para o bônus solar aparecer logo na demonstração
    "solar": {
        "power_kw": 0.0, "bonus_a": 0.0, "hour_of_day": 6.0, "coverage_pct": 0.0,
        "solar_energy_kwh": 0.0, "grid_energy_kwh": 0.0, "co2_avoided_kg": 0.0,
    },
}


def step():
    stations = state["stations"]
    active = [s for s in stations if s["active"]]
    pre = [s for s in active if s["plan"] == "pre"]
    pos = [s for s in active if s["plan"] == "pos"]

    # --- Energia renovável: geração solar do instante atual -----------------
    state["clock_tick"] += 1
    factor, hour_of_day = solar_output_factor(state["clock_tick"])
    solar_kw = SOLAR_INSTALLED_KWP * factor
    # a geração solar é convertida em amperes e vira um BÔNUS de capacidade
    # que o motor de gerenciamento de demanda soma à capacidade da rede —
    # é aqui que a energia renovável entra na lógica de automação, e não só
    # como um número decorativo na tela.
    solar_bonus_a = min((solar_kw * 1000) / VOLTAGE, GRID_CAPACITY_A * SOLAR_BONUS_CAP_RATIO)

    # --- Motor de gerenciamento de demanda -----------------------------------
    # O pré-pago sempre tem capacidade garantida dentro do limite da REDE
    # (contrato com a concessionária). O bônus solar amplia especificamente
    # a capacidade disponível para o pós-pago, que é quem se beneficia
    # diretamente da energia renovável gerada no momento.
    pre_target_total = sum(s["target_a"] for s in pre)
    pre_scale = GRID_CAPACITY_A / pre_target_total if pre_target_total > GRID_CAPACITY_A else 1
    reserved = min(pre_target_total, GRID_CAPACITY_A)
    remaining = max(GRID_CAPACITY_A - reserved, 0) + solar_bonus_a
    max_capacity_a = GRID_CAPACITY_A + solar_bonus_a  # capacidade total exibida no dashboard

    pos_target_total = sum(s["target_a"] for s in pos)
    pos_scale = min(remaining / pos_target_total, 1) if pos_target_total > 0 else 1

    is_overloaded = pos_target_total > remaining + 0.01
    if is_overloaded and not state["overload_flag"]:
        state["overload_events"] += 1
    state["overload_flag"] = is_overloaded

    for s in stations:
        if not s["active"]:
            s["current_a"] = 0.0
            s["status"] = "livre"
            continue
        jitter = random.uniform(0.9, 1.1)
        if s["plan"] == "pre":
            cur = s["target_a"] * pre_scale * jitter
            power = cur * VOLTAGE / 1000
            dE = power * TICK_HOURS
            s["current_a"] = cur
            s["status"] = "reservado" if pre_scale < 0.999 else "carregando"
            s["energy_kwh"] += dE
            s["cost"] += dE * PRICE_PRE
        else:
            cur = s["target_a"] * pos_scale * jitter
            power = cur * VOLTAGE / 1000
            dE = power * TICK_HOURS
            status = "carregando"
            if pos_scale <= 0.01:
                status = "em fila"
            elif pos_scale < 0.85:
                status = "limitado"
            s["current_a"] = cur
            s["status"] = status
            s["energy_kwh"] += dE
            s["cost"] += dE * PRICE_POS

    # --- Contabilização de energia solar x energia de rede -------------------
    total_power_kw = sum(s["current_a"] for s in stations) * VOLTAGE / 1000
    solar_used_kw = min(total_power_kw, solar_kw)
    grid_used_kw = max(total_power_kw - solar_kw, 0)
    coverage_pct = (solar_used_kw / total_power_kw * 100) if total_power_kw > 0 else 0.0

    sol = state["solar"]
    sol["power_kw"] = round(solar_kw, 2)
    sol["bonus_a"] = round(solar_bonus_a, 2)
    sol["hour_of_day"] = round(hour_of_day, 1)
    sol["coverage_pct"] = round(coverage_pct, 1)
    sol["solar_energy_kwh"] += solar_used_kw * TICK_HOURS
    sol["grid_energy_kwh"] += grid_used_kw * TICK_HOURS
    sol["co2_avoided_kg"] += solar_used_kw * TICK_HOURS * GRID_EMISSION_FACTOR_KG_KWH

    pre_revenue = sum(s["cost"] for s in stations if s["plan"] == "pre")
    pos_revenue = sum(s["cost"] for s in stations if s["plan"] == "pos")
    load = sum(s["current_a"] for s in stations)
    hist = state["history"]
    hist.append(dict(
        t=len(hist), pre=round(pre_revenue, 2), pos=round(pos_revenue, 2), load=round(load, 1),
        solar_kw=round(solar_used_kw, 2), grid_kw=round(grid_used_kw, 2),
    ))
    state["history"] = hist[-40:]
    state["max_capacity_a"] = round(max_capacity_a, 1)


def loop():
    while True:
        time.sleep(TICK_SECONDS)
        with lock:
            step()


# ---------------------------------------------------------------------------
# Servidor HTTP (só biblioteca padrão — sem Flask). Lê os arquivos de
# templates/ e static/ do disco a cada requisição.
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencia o log de cada requisição no terminal

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_file(self, path: Path, content_type):
        if not path.is_file():
            self._send(404, f"arquivo não encontrado: {path.name}")
            return
        self._send(200, path.read_text(encoding="utf-8"), content_type)

    def do_GET(self):
        if self.path == "/":
            self._send_file(TEMPLATES_DIR / "index.html", "text/html; charset=utf-8")
        elif self.path == "/style.css":
            self._send_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
        elif self.path == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
        elif self.path == "/chart.umd.js":
            self._send_file(STATIC_DIR / "chart.umd.js", "application/javascript; charset=utf-8")
        elif self.path == "/api/state":
            with lock:
                stations = state["stations"]
                payload = {
                    "stations": stations,
                    "history": state["history"],
                    "overload_events": state["overload_events"],
                    "total_load": sum(s["current_a"] for s in stations),
                    "total_revenue": sum(s["cost"] for s in stations),
                    "total_energy": sum(s["energy_kwh"] for s in stations),
                    "grid_capacity": GRID_CAPACITY_A,
                    "max_capacity": state.get("max_capacity_a", GRID_CAPACITY_A),
                    "voltage": VOLTAGE,
                    "price_pre": PRICE_PRE,
                    "price_pos": PRICE_POS,
                    "solar": {**state["solar"], "installed_kwp": SOLAR_INSTALLED_KWP},
                }
            self._send(200, json.dumps(payload), "application/json")
        else:
            self._send(404, "not found")

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "api" and parts[1] in ("toggle_plug", "toggle_plan"):
            try:
                sid = int(parts[2])
            except ValueError:
                self._send(400, "{}", "application/json")
                return
            with lock:
                for s in state["stations"]:
                    if s["id"] == sid:
                        if parts[1] == "toggle_plug":
                            s["active"] = not s["active"]
                            s["current_a"] = 0.0
                            s["status"] = "carregando" if s["active"] else "livre"
                        else:
                            s["plan"] = "pos" if s["plan"] == "pre" else "pre"
                            s["target_a"] = 10 if s["plan"] == "pos" else 16
            self._send(200, '{"ok": true}', "application/json")
        else:
            self._send(404, "not found")


def open_browser_later():
    time.sleep(0.8)
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=open_browser_later, daemon=True).start()
    print(f"ReCarga rodando em http://localhost:{PORT}  (Ctrl+C para parar)")
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    server.serve_forever()

