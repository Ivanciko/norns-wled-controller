"""system_control.py - estado de red y apagado.

Requiere /etc/sudoers.d/wled-controller con NOPASSWD para poweroff y nmcli,
ya que el proceso corre como systemd --user (sin sesion "activa" para
polkit) y por tanto no puede apagar sin sudo (nmcli ya no hace falta aqui,
pero se deja el NOPASSWD por si se vuelve a necesitar).

Sin escaneo ni conexion manual a otras redes desde el propio dispositivo -
quitado a proposito (2026-08-03): el Pi se conecta solo a la red domestica
configurada por prioridad en NetworkManager (ver perfiles wled-ctrl-*), sin
menu que pueda desconectarlo sin querer pidiendo una contrasena por error.
"""
import subprocess


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


def shutdown():
    try:
        subprocess.run(["sudo", "poweroff"], timeout=10)
    except Exception:
        pass
