"""system_control.py - estado de red, escaneo/conexion WiFi y apagado.

Requiere /etc/sudoers.d/wled-controller con NOPASSWD para poweroff y nmcli,
ya que el proceso corre como systemd --user (sin sesion "activa" para
polkit) y por tanto no puede apagar ni modificar conexiones sin sudo.
"""
import subprocess
import threading


def _nmcli(*args, timeout=15):
    try:
        return subprocess.run(
            ["nmcli", *args], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return subprocess.CompletedProcess(args, 1, "", "")


def get_network_status():
    """{"iface","ip","ssid"} de la conexion activa, o {"iface": None} si no hay."""
    result = _nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        device, dtype, state, connection = parts
        if state != "connected" or dtype not in ("ethernet", "wifi"):
            continue
        ip = ""
        ip_result = _nmcli("-t", "-f", "IP4.ADDRESS", "device", "show", device)
        for ip_line in ip_result.stdout.splitlines():
            if ip_line.startswith("IP4.ADDRESS"):
                ip = ip_line.split(":", 1)[1].split("/")[0]
                break
        return {"iface": device, "ip": ip, "ssid": connection if dtype == "wifi" else None}
    return {"iface": None, "ip": "", "ssid": None}


def scan_wifi():
    """Lista de {"ssid","signal","security"}, sin duplicados, ordenada por señal."""
    result = _nmcli(
        "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes",
        timeout=20,
    )
    seen = {}
    for line in result.stdout.splitlines():
        try:
            ssid_part, signal_str, security = line.rsplit(":", 2)
        except ValueError:
            continue
        ssid = ssid_part.replace("\\:", ":")
        if not ssid:
            continue
        try:
            signal = int(signal_str)
        except ValueError:
            signal = 0
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = {"ssid": ssid, "signal": signal, "security": security}
    return sorted(seen.values(), key=lambda n: n["signal"], reverse=True)


def connect_wifi(ssid, password, on_done):
    """Conecta en segundo plano; on_done(ok, mensaje) se llama al terminar."""
    def run():
        args = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            ok = result.returncode == 0
            msg = (result.stdout if ok else result.stderr).strip() or "error"
        except subprocess.TimeoutExpired:
            ok, msg = False, "timeout"
        except Exception as exc:
            ok, msg = False, str(exc)
        on_done(ok, msg)

    threading.Thread(target=run, daemon=True).start()


def shutdown():
    try:
        subprocess.run(["sudo", "poweroff"], timeout=10)
    except Exception:
        pass
