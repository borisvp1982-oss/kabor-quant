# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Borislav Vladimirov Petrakiev
"""
acceso.py -- Inicio de sesión y conexión segura del panel de KaBor Quant.

  * Usuarios en data/usuarios.json. La contraseña nunca se guarda: solo su huella scrypt (sal aleatoria,
    N=2^14, r=8, p=1), que es lenta a propósito para que probar contraseñas por fuerza bruta no sea viable.
  * Sesiones en data/sesiones.json. El navegador guarda un identificador aleatorio de 256 bits en una cookie
    HttpOnly + SameSite=Strict (+ Secure con HTTPS); en el archivo solo está su SHA-256, así que copiarlo no
    sirve para entrar. Sobreviven a los reinicios del programa.
  * Freno a intentos: 5 fallos en 15 minutos desde la misma dirección bloquean esa dirección 15 minutos.
  * Certificado autofirmado para HTTPS con openssl (incluido en Raspberry Pi OS).
  * Verificación en dos pasos opcional (7.3): códigos TOTP de 6 cifras (RFC 6238, los de Google Authenticator,
    Aegis, 1Password…) y 8 códigos de recuperación de un solo uso, guardados solo como huella.

Sin dependencias: solo la biblioteca estándar de Python.
"""
import base64
import hashlib
import hmac
import struct
import urllib.parse
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from collections import deque

COOKIE = "kq_sesion"
PIDE_CODIGO = "PIDE_CODIGO"
MIN_CLAVE = 10
NOMBRE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
FALLOS_MAX = 5                 # fallos permitidos en la ventana
VENTANA_S = 15 * 60            # ventana para contar fallos
BLOQUEO_S = 15 * 60            # bloqueo tras superar los fallos
GUARDAR_ACTIVIDAD_S = 300      # cada cuánto se escribe en disco la última actividad de una sesión


def huella_clave(clave, sal=None):
    sal = sal or secrets.token_bytes(16)
    dk = hashlib.scrypt(clave.encode("utf-8"), salt=sal, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P, sal.hex(), dk.hex())


def comprobar_clave(clave, huella):
    try:
        alg, n, r, p, sal, dk = huella.split("$")
        if alg != "scrypt":
            return False
        got = hashlib.scrypt(clave.encode("utf-8"), salt=bytes.fromhex(sal), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(dk) // 2)
        return hmac.compare_digest(got.hex(), dk)
    except (ValueError, TypeError):
        return False


def _sha(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------- verificación en dos pasos (TOTP) ----------------
TOTP_PASO = 30
TOTP_EMISOR = "KaBor Quant"


def totp_codigo(secreto, paso):
    clave = base64.b32decode(secreto.upper() + "=" * (-len(secreto) % 8))
    h = hmac.new(clave, struct.pack(">Q", paso), hashlib.sha1).digest()
    o = h[-1] & 15
    return "{:06d}".format((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000)


def totp_paso_valido(secreto, codigo, ahora=None, margen=1):
    """Paso de tiempo en que el código es válido (±margen pasos de 30 s), o None."""
    codigo = re.sub(r"\s", "", codigo or "")
    if not re.fullmatch(r"\d{6}", codigo):
        return None
    p = int((ahora if ahora is not None else time.time()) // TOTP_PASO)
    for d in range(-margen, margen + 1):
        if hmac.compare_digest(totp_codigo(secreto, p + d), codigo):
            return p + d
    return None


def _limpia_rec(c):
    return re.sub(r"[^a-z0-9]", "", (c or "").lower())


def _leer(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _escribir(path, obj):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Acceso:
    def __init__(self, carpeta, horas_inactividad=12, dias_recordar=30):
        self.f_usuarios = os.path.join(carpeta, "usuarios.json")
        self.f_sesiones = os.path.join(carpeta, "sesiones.json")
        self.idle_ms = int(horas_inactividad * 3_600_000)
        self.recordar_ms = int(dias_recordar * 86_400_000)
        self.lock = threading.Lock()
        self._mt = {}
        self.usuarios = self._cargar(self.f_usuarios, "usuarios")
        self.sesiones = self._cargar(self.f_sesiones, "sesiones")
        self.fallos = {}          # ip -> deque de instantes de fallo
        self.bloqueos = {}        # ip -> instante en que termina el bloqueo
        self._dummy = None        # huella de relleno para no revelar qué usuarios existen
        self._ultimo_guardado = 0
        with self.lock:
            self._purgar(int(time.time() * 1000))

    # ---------------- usuarios ----------------
    def hay_usuarios(self):
        with self.lock:
            self._sync()
            return bool(self.usuarios)

    def lista(self):
        with self.lock:
            self._sync()
            return sorted(self.usuarios)

    def poner_usuario(self, nombre, clave):
        """Crea el usuario o le cambia la contraseña. Cambiarla cierra sus sesiones abiertas."""
        nombre = (nombre or "").strip()
        if not NOMBRE_RE.match(nombre):
            raise ValueError("El usuario debe tener de 3 a 32 caracteres: letras, números, punto, guion o guion bajo")
        if len(clave or "") < MIN_CLAVE:
            raise ValueError("La contraseña debe tener al menos {} caracteres".format(MIN_CLAVE))
        if clave.lower() == nombre.lower():
            raise ValueError("La contraseña no puede ser igual que el usuario")
        ahora = int(time.time() * 1000)
        with self.lock:
            self._sync()
            nuevo = nombre not in self.usuarios
            u = self.usuarios.get(nombre, {"creado": ahora})
            u.update({"huella": huella_clave(clave), "cambiado": ahora})
            self.usuarios[nombre] = u
            _escribir(self.f_usuarios, {"usuarios": self.usuarios})
            self._mt[self.f_usuarios] = self._mtime(self.f_usuarios)
            if not nuevo:
                self._cerrar(lambda s: s["usuario"] == nombre)
        return nuevo

    def borrar_usuario(self, nombre):
        with self.lock:
            self._sync()
            if nombre not in self.usuarios:
                raise KeyError("No existe el usuario {}".format(nombre))
            del self.usuarios[nombre]
            _escribir(self.f_usuarios, {"usuarios": self.usuarios})
            self._mt[self.f_usuarios] = self._mtime(self.f_usuarios)
            self._cerrar(lambda s: s["usuario"] == nombre)

    # ---------------- inicio de sesión ----------------
    def espera(self, ip):
        """Segundos que le quedan de bloqueo a esa dirección (0 = puede intentarlo)."""
        fin = self.bloqueos.get(ip, 0)
        return max(0, int(fin - time.time()) + 1) if fin > time.time() else 0

    def entrar(self, nombre, clave, ip, agente="", recordar=False, codigo=None, sin_codigo=False):
        """Devuelve (token, None, 0, False) si es correcto o (None, mensaje, segundos_de_espera, bloqueo_nuevo).
        Con la verificación en dos pasos activa y sin código, devuelve (None, PIDE_CODIGO, 0, False)."""
        w = self.espera(ip)
        if w:
            return None, "Demasiados intentos fallidos. Espera unos minutos.", w, False
        with self.lock:
            self._sync()
            u = self.usuarios.get((nombre or "").strip())
        if u is None:
            if self._dummy is None:
                self._dummy = huella_clave(secrets.token_hex(16))
            comprobar_clave(clave or "", self._dummy)      # mismo tiempo que con un usuario real
            ok = False
        else:
            ok = comprobar_clave(clave or "", u["huella"])
        mal_codigo = False
        if ok and not sin_codigo and (u.get("totp") or {}).get("secreto"):
            if not (codigo or "").strip():
                return None, PIDE_CODIGO, 0, False      # contraseña buena: falta el código (no cuenta como fallo)
            if not self._codigo_ok(nombre.strip(), codigo):
                ok, mal_codigo = False, True
        if not ok:
            ahora = time.time()
            d = self.fallos.setdefault(ip, deque())
            d.append(ahora)
            while d and d[0] < ahora - VENTANA_S:
                d.popleft()
            if len(d) >= FALLOS_MAX:
                self.bloqueos[ip] = ahora + BLOQUEO_S
                d.clear()
                return None, "Demasiados intentos fallidos. Acceso bloqueado 15 minutos desde este dispositivo.", BLOQUEO_S, True
            quedan = FALLOS_MAX - len(d)
            return None, "{}. {} intento{} más antes del bloqueo.".format(
                "Código incorrecto o caducado" if mal_codigo else "Usuario o contraseña incorrectos",
                quedan, "" if quedan == 1 else "s"), 0, False
        self.fallos.pop(ip, None)
        token = secrets.token_urlsafe(32)
        ahora = int(time.time() * 1000)
        with self.lock:
            self.sesiones[_sha(token)] = {"usuario": nombre.strip(), "creada": ahora, "ultima": ahora,
                                          "recordar": bool(recordar), "ip": ip, "agente": (agente or "")[:160]}
            self.usuarios[nombre.strip()]["ultimo_acceso"] = ahora
            _escribir(self.f_usuarios, {"usuarios": self.usuarios})
            self._mt[self.f_usuarios] = self._mtime(self.f_usuarios)
            self._purgar(ahora)
            self._guardar(ahora)
        return token, None, 0, False

    def sesion(self, token):
        """Usuario de esa sesión, o None si no existe o ha caducado. Renueva la actividad."""
        if not token:
            return None
        h = _sha(token)
        ahora = int(time.time() * 1000)
        with self.lock:
            self._sync()
            s = self.sesiones.get(h)
            if s is None:
                return None
            if self._caducada(s, ahora) or s["usuario"] not in self.usuarios:
                del self.sesiones[h]
                self._guardar(ahora)
                return None
            s["ultima"] = ahora
            if ahora - self._ultimo_guardado > GUARDAR_ACTIVIDAD_S * 1000:
                self._guardar(ahora)
            return s["usuario"]

    def max_age(self, token):
        s = self.sesiones.get(_sha(token)) if token else None
        return self.recordar_ms // 1000 if s and s.get("recordar") else None

    def salir(self, token):
        if not token:
            return
        with self.lock:
            if self.sesiones.pop(_sha(token), None) is not None:
                self._guardar(int(time.time() * 1000))

    def cerrar_todas(self, usuario=None):
        with self.lock:
            self._sync()
            return self._cerrar(lambda s: usuario is None or s["usuario"] == usuario)

    # ---------------- gestión desde el panel ----------------
    def sesiones_de(self, usuario, token_actual):
        """Sesiones abiertas de ese usuario, la más reciente primero. «id» es un prefijo de la huella (no sirve
        para entrar) y «actual» marca la de este navegador."""
        h0 = _sha(token_actual) if token_actual else None
        ahora = int(time.time() * 1000)
        with self.lock:
            self._sync()
            self._purgar(ahora)
            out = [{"id": h[:16], "creada": s["creada"], "ultima": s["ultima"], "ip": s.get("ip", ""),
                    "agente": s.get("agente", ""), "recordar": bool(s.get("recordar")), "actual": h == h0}
                   for h, s in self.sesiones.items() if s["usuario"] == usuario]
        return sorted(out, key=lambda x: (not x["actual"], -x["ultima"]))

    def cerrar_sesion_id(self, usuario, sid):
        with self.lock:
            self._sync()
            if not sid or len(sid) < 8:
                return 0
            return self._cerrar_h([h for h, s in self.sesiones.items() if h.startswith(sid) and s["usuario"] == usuario])

    def cerrar_otras(self, usuario, token_actual):
        h0 = _sha(token_actual) if token_actual else None
        with self.lock:
            self._sync()
            return self._cerrar_h([h for h, s in self.sesiones.items() if s["usuario"] == usuario and h != h0])

    def cambiar_clave(self, usuario, actual, nueva):
        """Comprueba la contraseña actual y pone la nueva. Cierra todas las sesiones del usuario."""
        with self.lock:
            self._sync()
            u = self.usuarios.get(usuario)
        if not u or not comprobar_clave(actual or "", u["huella"]):
            raise ValueError("La contraseña actual no es correcta")
        if actual == nueva:
            raise ValueError("La contraseña nueva tiene que ser distinta de la actual")
        self.poner_usuario(usuario, nueva)

    # ---------------- verificación en dos pasos ----------------
    def totp_estado(self, usuario):
        with self.lock:
            self._sync()
            t = (self.usuarios.get(usuario) or {}).get("totp") or {}
        return {"activo": bool(t.get("secreto")), "desde": t.get("desde"), "recuperacion": len(t.get("rec") or [])}

    def totp_iniciar(self, usuario):
        """Genera un secreto nuevo, pendiente de confirmar con un código. Devuelve (secreto, enlace otpauth)."""
        secreto = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        with self.lock:
            self._sync()
            u = self.usuarios.get(usuario)
            if u is None:
                raise KeyError("No existe el usuario")
            u["totp_pend"] = {"secreto": secreto, "t": int(time.time() * 1000)}
            self._guardar_usuarios()
        etiqueta = urllib.parse.quote("{}:{}".format(TOTP_EMISOR, usuario))
        uri = "otpauth://totp/{}?secret={}&issuer={}&algorithm=SHA1&digits=6&period=30".format(
            etiqueta, secreto, urllib.parse.quote(TOTP_EMISOR))
        return secreto, uri

    def totp_confirmar(self, usuario, codigo):
        """Activa el secreto pendiente si el código es bueno. Devuelve los códigos de recuperación (solo esta vez)."""
        with self.lock:
            self._sync()
            u = self.usuarios.get(usuario) or {}
            pend = u.get("totp_pend")
        if not pend or int(time.time() * 1000) - pend["t"] > 30 * 60_000:
            raise ValueError("Ha pasado demasiado tiempo. Vuelve a empezar la activación.")
        paso = totp_paso_valido(pend["secreto"], codigo)
        if paso is None:
            raise ValueError("Ese código no es correcto. Mira que la hora del móvil esté en automático.")
        rec = ["{}-{}".format(secrets.token_hex(2), secrets.token_hex(2)) for _ in range(8)]
        with self.lock:
            u = self.usuarios.get(usuario)
            u["totp"] = {"secreto": pend["secreto"], "ultimo": paso, "desde": int(time.time() * 1000),
                         "rec": [_sha(_limpia_rec(c)) for c in rec]}
            u.pop("totp_pend", None)
            self._guardar_usuarios()
        return rec

    def totp_quitar(self, usuario, clave, codigo):
        with self.lock:
            self._sync()
            u = self.usuarios.get(usuario)
        if not u or not comprobar_clave(clave or "", u["huella"]):
            raise ValueError("La contraseña no es correcta")
        if not self._codigo_ok(usuario, codigo):
            raise ValueError("El código no es correcto")
        self.totp_quitar_forzado(usuario)

    def totp_quitar_forzado(self, usuario):
        """Desde la Raspberry (./bot.sh quitar-2fa), por si se pierde el móvil."""
        with self.lock:
            self._sync()
            u = self.usuarios.get(usuario)
            if u is None:
                raise KeyError("No existe el usuario {}".format(usuario))
            u.pop("totp", None)
            u.pop("totp_pend", None)
            self._guardar_usuarios()

    def _codigo_ok(self, usuario, codigo):
        """Código de 6 cifras (no se puede repetir el mismo) o uno de recuperación (se gasta)."""
        with self.lock:
            self._sync()
            t = (self.usuarios.get(usuario) or {}).get("totp")
            if not t:
                return False
            paso = totp_paso_valido(t["secreto"], codigo)
            if paso is not None:
                if paso <= t.get("ultimo", -1):
                    return False
                t["ultimo"] = paso
                self._guardar_usuarios()
                return True
            h = _sha(_limpia_rec(codigo))
            if len(_limpia_rec(codigo)) == 8 and h in (t.get("rec") or []):
                t["rec"].remove(h)
                self._guardar_usuarios()
                return True
        return False

    def _guardar_usuarios(self):
        _escribir(self.f_usuarios, {"usuarios": self.usuarios})
        self._mt[self.f_usuarios] = self._mtime(self.f_usuarios)

    # ---------------- internos ----------------
    def _cerrar_h(self, hashes):
        for h in hashes:
            self.sesiones.pop(h, None)
        self._guardar(int(time.time() * 1000))
        return len(hashes)

    def _mtime(self, path):
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def _cargar(self, path, clave):
        self._mt[path] = self._mtime(path)
        return _leer(path, {}).get(clave, {})

    def _sync(self):
        """Si otro proceso (./bot.sh usuario, cerrar-sesiones) cambió los archivos, se vuelven a leer."""
        if self._mtime(self.f_usuarios) != self._mt.get(self.f_usuarios):
            self.usuarios = self._cargar(self.f_usuarios, "usuarios")
        if self._mtime(self.f_sesiones) != self._mt.get(self.f_sesiones):
            self.sesiones = self._cargar(self.f_sesiones, "sesiones")

    def _caducada(self, s, ahora):
        if s.get("recordar"):
            return ahora - s["creada"] > self.recordar_ms
        return ahora - s["ultima"] > self.idle_ms

    def _purgar(self, ahora):
        for h in [h for h, s in self.sesiones.items() if self._caducada(s, ahora)]:
            del self.sesiones[h]

    def _cerrar(self, cond):
        quitar = [h for h, s in self.sesiones.items() if cond(s)]
        for h in quitar:
            del self.sesiones[h]
        self._guardar(int(time.time() * 1000))
        return len(quitar)

    def _guardar(self, ahora):
        self._ultimo_guardado = ahora
        try:
            _escribir(self.f_sesiones, {"sesiones": self.sesiones})
            self._mt[self.f_sesiones] = self._mtime(self.f_sesiones)
        except OSError:
            pass


# ---------------- certificado HTTPS ----------------
def ips_locales():
    out = {"127.0.0.1"}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        out.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        out.update(x for x in r.stdout.split() if re.match(r"^\d+\.\d+\.\d+\.\d+$", x))
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(out)


def crear_certificado(cert, clave, nombres_extra=()):
    """Certificado autofirmado (ECDSA P-256, 825 días) válido para el nombre de la Raspberry, nombre.local,
    localhost y sus IP. Devuelve la lista de nombres incluidos."""
    os.makedirs(os.path.dirname(cert) or ".", exist_ok=True)
    host = socket.gethostname()
    dns = [host, host + ".local", "localhost"] + [n for n in nombres_extra if n]
    ips = ips_locales()
    san = ",".join(["DNS:" + d for d in dict.fromkeys(dns)] + ["IP:" + i for i in ips])
    cmd = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
           "-keyout", clave, "-out", cert, "-days", "825", "-subj", "/CN=KaBor Quant ({})".format(host),
           "-addext", "subjectAltName=" + san, "-addext", "extendedKeyUsage=serverAuth"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError("No encuentro openssl. Instálalo con: sudo apt install openssl")
    if r.returncode != 0:
        raise RuntimeError("openssl no pudo crear el certificado: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    os.chmod(clave, 0o600)
    return list(dict.fromkeys(dns)) + ips
