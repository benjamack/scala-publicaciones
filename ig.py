#!/usr/bin/env python3
"""
ig.py - la cuenta de Instagram de Scala, por la API oficial de Meta
(Instagram API with Instagram Login). Sin dependencias.

Lee la cuenta, renueva el token, arma el repo público de las publicaciones y,
desde ese repo, publica cada pieza en su día y responde a quien comenta SCALA.

Uso (en esta carpeta)
  python3 scripts/ig.py verificar    # ¿anda el token? ¿de qué cuenta es? ¿qué puede hacer?
  python3 scripts/ig.py refrescar    # renueva el token de 60 días y lo guarda en .env
  python3 scripts/ig.py preparar --destino <carpeta>
                                     # arma el repo público en <carpeta> (un clon): agenda.json,
                                     # piezas/, ig.py, el workflow y el README. No pisa publicadas.json
  python3 scripts/ig.py subir-token --repo <dueño/nombre>
                                     # guarda el token de .env como secreto del repo (con gh, por stdin)

Uso (en el repo público; GitHub Actions corre "agenda")
  python3 ig.py agenda [--carpeta .] [--ahora <ISO>] [--en-seco]
                                     # publica como mucho UNA pieza que ya llegó a su hora y responde
                                     # a quien comenta SCALA (en las últimas 30 publicaciones y en las
                                     # historias activas) o la escribe por mensaje (sólo si el mensaje es
                                     # la palabra; "por_mensaje": false en la agenda lo apaga).
                                     # --en-seco no llama a la API ni escribe
  python3 ig.py publicar <id> [--carpeta .]
                                     # publica esa pieza ya, aunque no sea su hora (exige su OK)
  python3 ig.py probar <id> [--carpeta .]
                                     # crea los contenedores y espera que Instagram baje los archivos,
                                     # sin publicar: la prueba de punta a punta

El token sale de IG_ACCESS_TOKEN: del entorno y, si no está, del archivo .env de
esta carpeta. No se imprime nunca y sólo viaja a graph.instagram.com. En un repo
público tampoco se imprime ni se guarda el texto de un comentario ni quién lo
escribió: sólo ids y cantidades. Lo mismo vale para los mensajes.
"""

import argparse
import copy
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(RAIZ, ".env")
HOST = "https://graph.instagram.com"
VERSION = "v25.0"

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

# Códigos de error de la API de Meta que significan lo mismo siempre.
SIN_PERMISO = {10, 200}
TOKEN_INVALIDO = {190}
LIMITE_DE_LLAMADAS = {4, 17, 32, 613}

LANZAMIENTO = os.path.join(RAIZ, "instagram", "lanzamiento")
PUBLICO = os.path.join(RAIZ, "scripts", "publico")

INTERVALO_ESPERA = 10         # segundos entre una consulta y otra del estado de un contenedor
ESPERA_MAXIMA = 360           # 6 minutos
INTENTOS_MAXIMOS = 3
RESPUESTAS_POR_CORRIDA = 25
PUBLICACIONES_A_MIRAR = 30
CONVERSACIONES_A_MIRAR = 20
HORAS_DE_CONVERSACION = 2     # sólo se abren las conversaciones actualizadas hace menos que esto
HORAS_DE_MENSAJE = 23         # la ventana de Meta para responder un mensaje es de 24 horas


class ErrorDeApi(Exception):
    def __init__(self, status, cuerpo):
        error = cuerpo.get("error", {}) if isinstance(cuerpo, dict) else {}
        self.status = status
        self.codigo = error.get("code")
        self.mensaje = error.get("message") or "sin detalle"
        super().__init__(self.mensaje)


def salir(mensaje, codigo=2):
    print(mensaje, file=sys.stderr)
    sys.exit(codigo)


def leer_env(path=ENV):
    """CLAVE=valor por línea. Sin expansión ni comillas mágicas."""
    valores = {}
    if not os.path.exists(path):
        return valores
    with open(path, encoding="utf-8") as fh:
        for linea in fh:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            valores[clave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def guardar_env(cambios, path=ENV):
    """Reemplaza o agrega claves y deja el resto del archivo como estaba."""
    lineas = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lineas = fh.read().splitlines()
    pendientes = dict(cambios)
    for i, linea in enumerate(lineas):
        clave = linea.split("=", 1)[0].strip()
        if "=" in linea and not linea.lstrip().startswith("#") and clave in pendientes:
            lineas[i] = f"{clave}={pendientes.pop(clave)}"
    lineas += [f"{clave}={valor}" for clave, valor in pendientes.items()]
    # Se escribe aparte y se cambia de un golpe: un corte a la mitad no deja el
    # token viejo perdido y el nuevo sin guardar.
    temporal = path + ".tmp"
    descriptor = os.open(temporal, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lineas) + "\n")
    os.replace(temporal, path)


def base():
    """El token no sale de esta máquina salvo hacia Instagram. IG_API_BASE existe
    para probar contra un servidor falso, y sólo acepta uno local."""
    destino = os.environ.get("IG_API_BASE", HOST).rstrip("/")
    partes = urllib.parse.urlparse(destino)
    es_local = partes.scheme == "http" and partes.hostname in ("127.0.0.1", "localhost")
    if destino != HOST and not es_local:
        salir("IG_API_BASE sólo puede ser graph.instagram.com o un servidor de prueba en esta máquina.")
    return destino


def pedir(token, ruta, parametros=None, token_en_url=False):
    """Un GET a la API. El token va en el encabezado; en la URL sólo donde la
    documentación de Meta lo pide así. Un error nunca arrastra la URL."""
    parametros = dict(parametros or {})
    encabezados = {"Accept": "application/json", "User-Agent": "scala-instagram/1.0"}
    if token_en_url:
        parametros["access_token"] = token
    else:
        encabezados["Authorization"] = "Bearer " + token
    url = base() + ruta + ("?" + urllib.parse.urlencode(parametros) if parametros else "")
    return _abrir(urllib.request.Request(url, headers=encabezados))


def _abrir(pedido):
    """Manda el pedido y devuelve el JSON. Un error nunca arrastra la URL."""
    try:
        with urllib.request.urlopen(pedido, timeout=30) as respuesta:
            return json.loads(respuesta.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        try:
            cuerpo = json.loads(error.read().decode("utf-8"))
        except ValueError:
            cuerpo = None
        raise ErrorDeApi(error.code, cuerpo) from None
    except urllib.error.URLError as error:
        raise ErrorDeApi(0, {"error": {"message": f"no se pudo conectar ({error.reason})"}}) from None
    except (OSError, ValueError):
        raise ErrorDeApi(0, {"error": {"message": "la respuesta de Instagram no llegó entera"}}) from None


def explicar(error, token):
    """Qué pasó y qué hacer, sin el token aunque Meta lo repitiera en su mensaje."""
    detalle = error.mensaje.replace(token, "(token)")
    if error.status == 0:
        return f"No hay conexión con Instagram: {detalle}."
    if error.codigo in TOKEN_INVALIDO:
        return ("El token no sirve o ya venció. Generá otro en el panel de Meta "
                "(Instagram > API setup with Instagram login > Generate token) y pegalo en .env.\n"
                f"  Meta dijo: {detalle}")
    if error.codigo in LIMITE_DE_LLAMADAS:
        return f"Instagram pidió esperar (límite de llamadas). Probá de nuevo en una hora.\n  Meta dijo: {detalle}"
    return f"Instagram contestó con un error (HTTP {error.status}).\n  Meta dijo: {detalle}"


def fecha(dia):
    return f"{dia.day} de {MESES[dia.month - 1]} de {dia.year}"


def buscar_token():
    return os.environ.get("IG_ACCESS_TOKEN") or leer_env().get("IG_ACCESS_TOKEN", "")


def token_o_salir():
    token = buscar_token()
    if not token:
        salir("Falta el token. Copiá .env.example como .env, en esta carpeta, y pegá el token "
              "después de IG_ACCESS_TOKEN=")
    if token.startswith("EAA"):
        salir("Ese token es de Facebook Login (empieza con EAA). Esta carpeta usa Instagram Login: "
              "el token sale de Instagram > API setup with Instagram login, en el panel de la app.")
    return token


def verificar():
    token = token_o_salir()
    campos = {"fields": "user_id,username,account_type,media_count"}
    en_url = False
    try:
        yo = pedir(token, f"/{VERSION}/me", campos)
    except ErrorDeApi as error:
        if error.status == 0:
            salir(explicar(error, token), 1)
        # La guía de inicio de Meta pasa el token como parámetro. Si con el
        # encabezado no alcanzó, se prueba de esa forma antes de darlo por malo.
        try:
            yo = pedir(token, f"/{VERSION}/me", campos, token_en_url=True)
            en_url = True
        except ErrorDeApi as segundo:
            salir(explicar(segundo, token), 1)

    cuenta = yo.get("user_id") or "me"
    print(f"Conectado a @{yo.get('username', '?')}")
    print(f"  tipo de cuenta   {yo.get('account_type', '?')}")
    print(f"  publicaciones    {yo.get('media_count', '?')}")

    try:
        limite = pedir(token, f"/{VERSION}/{cuenta}/content_publishing_limit",
                       {"fields": "config,quota_usage"}, token_en_url=en_url)
        dato = (limite.get("data") or [{}])[0]
        total = (dato.get("config") or {}).get("quota_total")
        usadas = dato.get("quota_usage", 0)
        cupo = f"{usadas} de {total}" if total else f"{usadas}"
        print(f"  publicar         sí · {cupo} usadas en las últimas 24 h")
    except ErrorDeApi as error:
        if error.codigo in SIN_PERMISO:
            print("  publicar         no · al token le falta el permiso instagram_business_content_publish")
        else:
            print(f"  publicar         no se pudo comprobar · {error.mensaje.replace(token, '(token)')}")

    try:
        medios = pedir(token, f"/{VERSION}/{cuenta}/media", {"fields": "id,comments_count", "limit": 1},
                       token_en_url=en_url)
        primera = (medios.get("data") or [{}])[0]
        primero = primera.get("id")
        if not primero:
            print("  comentarios      sin comprobar · la cuenta todavía no tiene publicaciones")
        else:
            entregados = pedir(token, f"/{VERSION}/{primero}/comments", {"limit": 1},
                               token_en_url=en_url).get("data") or []
            contados = primera.get("comments_count")
            if isinstance(contados, int) and contados > 0 and not entregados:
                print("  comentarios      los cuenta y no los entrega · la app de Meta parece estar en modo "
                      "desarrollo (sólo ve a quien tiene un rol en la app)")
            else:
                print("  comentarios      sí · puede leerlos y responderlos")
    except ErrorDeApi as error:
        if error.codigo in SIN_PERMISO:
            print("  comentarios      no · al token le falta el permiso instagram_business_manage_comments")
        else:
            print(f"  comentarios      no se pudo comprobar · {error.mensaje.replace(token, '(token)')}")

    try:
        pedir(token, f"/{VERSION}/{cuenta}/conversations", {"platform": "instagram", "limit": 1},
              token_en_url=en_url)
        print("  mensajes         sí · puede leer las conversaciones")
    except ErrorDeApi as error:
        if error.codigo in SIN_PERMISO:
            print("  mensajes         no · al token le falta el permiso instagram_business_manage_messages")
        else:
            print(f"  mensajes         no se pudo comprobar · {error.mensaje.replace(token, '(token)')}")

    vence = leer_env().get("IG_TOKEN_EXPIRES_AT")
    if vence:
        dia = datetime.date.fromisoformat(vence)
        faltan = (dia - datetime.date.today()).days
        print(f"  token            vence el {fecha(dia)} (faltan {faltan} días)")
    else:
        print("  token            dura 60 días desde que lo generaste · "
              "\"refrescar\" lo renueva (a partir de mañana) y anota la fecha")
    return 0


def refrescar():
    token = token_o_salir()
    try:
        # Acá Meta pide el token como parámetro; no hay forma con encabezado.
        nuevo = pedir(token, "/refresh_access_token", {"grant_type": "ig_refresh_token"},
                      token_en_url=True)
    except ErrorDeApi as error:
        salir(explicar(error, token) + "\n  Un token se puede renovar cuando tiene más de 24 horas "
              "y todavía no venció.", 1)
    if not nuevo.get("access_token"):
        salir("Instagram no devolvió un token nuevo. No se cambió nada.", 1)
    dia = datetime.date.today() + datetime.timedelta(seconds=int(nuevo.get("expires_in") or 0))
    guardar_env({"IG_ACCESS_TOKEN": nuevo["access_token"], "IG_TOKEN_EXPIRES_AT": dia.isoformat()})
    print(f"Token renovado y guardado en .env. Vence el {fecha(dia)}.")
    return 0


# --- Publicar ---------------------------------------------------------------

class ErrorDePublicacion(Exception):
    """Algo falló al publicar y se sabe decir qué. El mensaje no lleva token."""


def enviar(token, ruta, datos, como_json=False):
    """Un POST a la API. El token va en el cuerpo, nunca en la URL: los parámetros
    salen como formulario, o como JSON donde la API lo pide (mensajes)."""
    cuerpo = dict(datos)
    cuerpo["access_token"] = token
    encabezados = {"Accept": "application/json", "User-Agent": "scala-instagram/1.0"}
    if como_json:
        octetos = json.dumps(cuerpo).encode("utf-8")
        encabezados["Content-Type"] = "application/json"
        encabezados["Authorization"] = "Bearer " + token
    else:
        octetos = urllib.parse.urlencode(cuerpo).encode("utf-8")
        encabezados["Content-Type"] = "application/x-www-form-urlencoded"
    return _abrir(urllib.request.Request(base() + ruta, data=octetos, headers=encabezados, method="POST"))


def limpio(texto, token):
    """Un texto sin el token ni direcciones, para imprimirlo o guardarlo."""
    texto = str(texto)
    if token:
        texto = texto.replace(token, "(token)")
    return re.sub(r"https?://\S+", "(url)", texto)


def texto_de_error(error, token):
    if isinstance(error, ErrorDeApi):
        cabeza = "sin conexión" if error.status == 0 else f"HTTP {error.status}"
        codigo = f", código {error.codigo}" if error.codigo else ""
        return limpio(f"{cabeza}{codigo}: {error.mensaje}", token)
    return limpio(error, token)


def instante(texto):
    """Una fecha ISO 8601 con zona, también «+0000» y «Z» como las manda Instagram."""
    t = str(texto).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t)
    momento = datetime.datetime.fromisoformat(t)
    if momento.tzinfo is None:
        raise ValueError("falta la zona horaria")
    return momento


def escribir_atomico(path, texto):
    """Se escribe aparte y se cambia de un golpe: un corte no deja el archivo a medias."""
    temporal = path + ".tmp"
    with open(temporal, "w", encoding="utf-8") as fh:
        fh.write(texto)
    os.replace(temporal, path)


def como_json(datos):
    return json.dumps(datos, ensure_ascii=False, indent=2) + "\n"


class Cuenta:
    """La conexión a la cuenta: el token, quién es y cómo se le manda el token
    en los GET (decidido una vez, contra /me)."""

    def __init__(self, token):
        self.token = token
        self.en_url = False
        self.id = None
        self.usuario = ""

    def conectar(self):
        campos = {"fields": "user_id,username"}
        try:
            yo = pedir(self.token, f"/{VERSION}/me", campos)
        except ErrorDeApi as error:
            if error.status == 0:
                raise
            yo = pedir(self.token, f"/{VERSION}/me", campos, token_en_url=True)
            self.en_url = True
        self.id = yo.get("user_id") or yo.get("id")
        if not self.id:
            raise ErrorDePublicacion("Instagram no dijo de qué cuenta es el token.")
        self.usuario = yo.get("username") or ""
        return self

    def leer(self, ruta, parametros=None):
        return pedir(self.token, f"/{VERSION}{ruta}", parametros, token_en_url=self.en_url)

    def mandar(self, ruta, datos, como_json=False):
        return enviar(self.token, f"/{VERSION}{ruta}", datos, como_json)


def url_de(base_url, pieza, archivo):
    return base_url.rstrip("/") + "/piezas/" + pieza["id"] + "/" + urllib.parse.quote(archivo)


def es_video(archivo):
    return archivo.lower().endswith((".mp4", ".mov"))


def crear_contenedor(cuenta, datos, que):
    try:
        creado = cuenta.mandar(f"/{cuenta.id}/media", datos)
    except ErrorDeApi as error:
        raise ErrorDePublicacion(f"{que}: {texto_de_error(error, cuenta.token)}") from None
    if not creado.get("id"):
        raise ErrorDePublicacion(f"{que}: Instagram no devolvió el contenedor.")
    return creado["id"]


def esperar_listo(cuenta, contenedor, que):
    limite = time.monotonic() + ESPERA_MAXIMA
    while True:
        try:
            estado = cuenta.leer(f"/{contenedor}", {"fields": "status_code"}).get("status_code")
        except ErrorDeApi as error:
            raise ErrorDePublicacion(f"{que}: {texto_de_error(error, cuenta.token)}") from None
        if estado in ("FINISHED", "PUBLISHED"):
            return
        if estado in ("ERROR", "EXPIRED"):
            raise ErrorDePublicacion(f"{que}: Instagram no pudo procesarlo ({estado}).")
        if time.monotonic() >= limite:
            raise ErrorDePublicacion(f"{que}: Instagram no terminó de procesarlo a tiempo.")
        time.sleep(INTERVALO_ESPERA)


def armar_contenedor(cuenta, pieza, base_url, avisar=None):
    """Crea el contenedor de la pieza (y los de sus ítems) y espera a que Instagram
    haya bajado los archivos. Devuelve el id del contenedor que se publica."""
    avisar = avisar or (lambda texto: None)
    archivos = pieza["archivos"]
    caption = pieza.get("caption") or ""
    tipo = pieza["tipo"]
    if tipo == "reel":
        datos = {"media_type": "REELS", "video_url": url_de(base_url, pieza, archivos[0]),
                 "share_to_feed": "true", "caption": caption}
        que = archivos[0]
        if pieza.get("tapa"):
            datos["cover_url"] = url_de(base_url, pieza, pieza["tapa"])
            que = f"{archivos[0]} y {pieza['tapa']}"
        contenedor = crear_contenedor(cuenta, datos, que)
        esperar_listo(cuenta, contenedor, que)
        avisar(f"{que}: Instagram los pudo bajar")
        return contenedor
    if tipo == "story":
        archivo = archivos[0]
        datos = {"media_type": "STORIES"}
        datos["video_url" if es_video(archivo) else "image_url"] = url_de(base_url, pieza, archivo)
        contenedor = crear_contenedor(cuenta, datos, archivo)
        esperar_listo(cuenta, contenedor, archivo)
        avisar(f"{archivo}: Instagram lo pudo bajar")
        return contenedor
    if tipo == "carrusel":
        hijos = []
        for i, archivo in enumerate(archivos):
            datos = {"image_url": url_de(base_url, pieza, archivo), "is_carousel_item": "true"}
            if i == 0 and pieza.get("alt"):
                datos["alt_text"] = pieza["alt"]
            hijo = crear_contenedor(cuenta, datos, archivo)
            esperar_listo(cuenta, hijo, archivo)
            avisar(f"{archivo}: Instagram lo pudo bajar")
            hijos.append(hijo)
        padre = crear_contenedor(cuenta, {"media_type": "CAROUSEL", "children": ",".join(hijos),
                                          "caption": caption}, "el carrusel")
        esperar_listo(cuenta, padre, "el carrusel")
        return padre
    raise ErrorDePublicacion(f"No sé publicar una pieza de tipo «{tipo}».")


def publicar_contenedor(cuenta, contenedor):
    """Publica y devuelve (media_id, permalink). Que no se pueda leer el permalink
    no deshace la publicación."""
    try:
        publicado = cuenta.mandar(f"/{cuenta.id}/media_publish", {"creation_id": contenedor})
    except ErrorDeApi as error:
        raise ErrorDePublicacion(f"al publicar: {texto_de_error(error, cuenta.token)}") from None
    media_id = publicado.get("id")
    if not media_id:
        raise ErrorDePublicacion("al publicar: Instagram no devolvió el id de la publicación.")
    return media_id, permalink_de(cuenta, media_id)


def permalink_de(cuenta, media_id):
    try:
        return cuenta.leer(f"/{media_id}", {"fields": "permalink"}).get("permalink")
    except ErrorDeApi:
        return None


def publicaciones_recientes(cuenta):
    medios = cuenta.leer(f"/{cuenta.id}/media", {"fields": "id,caption,timestamp,comments_count",
                                                  "limit": PUBLICACIONES_A_MIRAR})
    return medios.get("data") or []


# --- El estado: qué salió y a quién se le respondió -------------------------

class Estado:
    def __init__(self, path):
        self.path = path
        datos = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                datos = json.load(fh)
        self.datos = {"publicadas": datos.get("publicadas") or {}, "respondidos": datos.get("respondidos") or []}
        self.original = copy.deepcopy(self.datos)

    @property
    def publicadas(self):
        return self.datos["publicadas"]

    @property
    def respondidos(self):
        return self.datos["respondidos"]

    def guardar(self):
        """Sólo si cambió algo."""
        if self.datos != self.original:
            escribir_atomico(self.path, como_json(self.datos))
            self.original = copy.deepcopy(self.datos)


def cargar_agenda(carpeta):
    path = os.path.join(carpeta, "agenda.json")
    if not os.path.exists(path):
        salir(f"No encuentro agenda.json en {carpeta}. Se arma con «preparar».")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pieza_de(agenda, id_pieza):
    for pieza in agenda["piezas"]:
        if pieza["id"] == id_pieza:
            return pieza
    salir(f"«{id_pieza}» no está en la agenda (o no tiene su OK).", 1)


def publicar_una(cuenta, agenda, pieza, estado, ahora):
    """Publica la pieza, salvo que ya esté en la cuenta con el mismo caption. Deja
    el resultado en el estado y devuelve «publicada», «igual» o «error»."""
    id_pieza = pieza["id"]
    intentos = (estado.publicadas.get(id_pieza) or {}).get("intentos", 0) + 1
    try:
        igual = next((m for m in publicaciones_recientes(cuenta)
                      if (m.get("caption") or "").strip() == pieza["caption"].strip()), None)
        if igual:
            estado.publicadas[id_pieza] = {
                "estado": "publicada", "media_id": igual["id"], "permalink": permalink_de(cuenta, igual["id"]),
                "cuando_salio": igual.get("timestamp"), "intentos": intentos - 1, "error": None}
            print(f"{id_pieza}: ya estaba publicada en la cuenta. Se marca como publicada y no se vuelve a publicar.")
            return "igual"
        contenedor = armar_contenedor(cuenta, pieza, agenda["base"])
        media_id, permalink = publicar_contenedor(cuenta, contenedor)
    except (ErrorDeApi, ErrorDePublicacion) as error:
        registrar_fallo(estado, id_pieza, intentos, texto_de_error(error, cuenta.token))
        return "error"
    estado.publicadas[id_pieza] = {
        "estado": "publicada", "media_id": media_id, "permalink": permalink,
        "cuando_salio": ahora.isoformat(timespec="seconds"), "intentos": intentos, "error": None}
    print(f"{id_pieza}: publicada" + (f" · {permalink}" if permalink else ""))
    return "publicada"


def registrar_fallo(estado, id_pieza, intentos, mensaje):
    """Suma un intento. A los INTENTOS_MAXIMOS la pieza queda en «falló» y no se reintenta."""
    definitivo = intentos >= INTENTOS_MAXIMOS
    estado.publicadas[id_pieza] = {
        "estado": "falló" if definitivo else "pendiente", "media_id": None, "permalink": None,
        "cuando_salio": None, "intentos": intentos, "error": mensaje}
    print(f"{id_pieza}: no se pudo publicar (intento {intentos} de {INTENTOS_MAXIMOS}). {mensaje}")
    if definitivo:
        print(f"{id_pieza}: queda en «falló» y no se reintenta. Con «publicar {id_pieza}» se puede forzar.")


def en_seco(agenda, estado, ahora):
    print(f"En seco: no se llama a la API ni se escribe nada. Ahora: {ahora.isoformat(timespec='seconds')}")
    tarde_maxima = datetime.timedelta(hours=agenda.get("tarde_maxima_horas", 36))
    hubo = False
    for pieza in sorted(agenda["piezas"], key=lambda p: instante(p["cuando"])):
        if not pieza.get("ok") or instante(pieza["cuando"]) > ahora:
            continue
        previo = (estado.publicadas.get(pieza["id"]) or {}).get("estado")
        if previo in ("publicada", "vencida", "falló"):
            continue
        if ahora - instante(pieza["cuando"]) > tarde_maxima:
            print(f"{pieza['id']}: vencería (más de {agenda.get('tarde_maxima_horas', 36)} h tarde) y no se publica.")
            continue
        print(f"Publicaría {pieza['id']} como {pieza['tipo']} ({pieza['cuando']}):")
        for archivo in pieza["archivos"]:
            print(f"  {archivo:<12} {url_de(agenda['base'], pieza, archivo)}")
        if pieza.get("tapa"):
            print(f"  {'tapa':<12} {url_de(agenda['base'], pieza, pieza['tapa'])}")
        print("  (antes miraría que el caption no esté ya en la cuenta)")
        hubo = True
        break
    if not hubo:
        print("No hay nada para publicar.")
    print("Las respuestas de SCALA (comentarios, historias y mensajes) no se revisan en seco.")
    return 0


class _TresFallos(Exception):
    """Tres respuestas seguidas fallaron: se corta todo y se sigue en la próxima corrida."""


def responder_scala(cuenta, agenda, estado, ahora):
    """Le manda el link por privado a quien comentó la palabra, en las últimas publicaciones
    (tengan la edad que tengan: el límite de Meta corre desde el comentario) y en las historias
    activas. Del texto ajeno sólo se compara con la palabra: no se guarda ni se imprime, ni
    quién lo escribió."""
    scala = agenda.get("scala")
    if not scala:
        return
    palabra = re.compile(r"\b" + re.escape(scala["palabra"]) + r"\b", re.IGNORECASE)
    dias = datetime.timedelta(days=scala.get("dias", 7))
    propio = cuenta.usuario.lower()
    vistos = respondidos = fallos = mudas = 0

    def revisar(id_medio):
        """Responde los comentarios de una publicación o historia y devuelve cuántos entregó Instagram."""
        nonlocal vistos, respondidos, fallos
        comentarios = cuenta.leer(f"/{id_medio}/comments",
                                  {"fields": "id,text,timestamp,username", "limit": 50}).get("data") or []
        for comentario in comentarios:
            id_comentario = comentario.get("id")
            if not id_comentario or id_comentario in estado.respondidos:
                continue
            if not palabra.search(comentario.get("text") or ""):
                continue
            if (comentario.get("username") or "").lower() == propio:
                continue
            try:
                if ahora - instante(comentario["timestamp"]) >= dias:
                    continue
            except (KeyError, ValueError):
                continue
            vistos += 1
            if respondidos >= RESPUESTAS_POR_CORRIDA:
                continue
            try:
                cuenta.mandar(f"/{cuenta.id}/messages",
                              {"recipient": {"comment_id": id_comentario},
                               "message": {"text": scala["mensaje"]}}, como_json=True)
            except ErrorDeApi as error:
                if error.codigo in SIN_PERMISO or error.codigo in LIMITE_DE_LLAMADAS or error.codigo in TOKEN_INVALIDO:
                    raise
                fallos += 1
                if fallos >= 3:
                    print("SCALA: tres respuestas seguidas fallaron; se sigue en la próxima corrida.")
                    raise _TresFallos() from None
                continue
            respondidos += 1
            estado.respondidos.append(id_comentario)
            estado.guardar()
        return len(comentarios)

    try:
        for medio in publicaciones_recientes(cuenta):
            contados = medio.get("comments_count")
            if not medio.get("id") or contados == 0:
                continue
            entregados = revisar(medio["id"])
            if isinstance(contados, int) and contados > 0 and entregados == 0:
                mudas += 1
        # Las historias son un tramo aparte: si Instagram no deja leerlas, no frena nada.
        try:
            historias = cuenta.leer(f"/{cuenta.id}/stories", {"fields": "id,timestamp",
                                                             "limit": PUBLICACIONES_A_MIRAR}).get("data") or []
            for historia in historias:
                if historia.get("id"):
                    revisar(historia["id"])
        except ErrorDeApi:
            pass
    except _TresFallos:
        return
    except ErrorDeApi as error:
        if error.codigo in SIN_PERMISO:
            print("SCALA: Instagram negó el permiso para leer o responder comentarios "
                  "(instagram_business_manage_comments, y mensajes). No se responde nada.")
        else:
            print(f"SCALA: no se pudo seguir ({texto_de_error(error, cuenta.token)}).")
    print(f"SCALA: {vistos} comentarios para responder, {respondidos} respondidos"
          + (f", {fallos} que no se pudieron" if fallos else "") + ".")
    if mudas:
        cuantas = "1 publicación" if mudas == 1 else f"{mudas} publicaciones"
        print(f"SCALA: Instagram cuenta comentarios en {cuantas} y no entrega ninguno. Lo más probable: "
              "la app de Meta sigue en modo desarrollo y sólo ve a quien tiene un rol en la app.")


def solo_la_palabra(texto, palabra):
    """True si el texto es la palabra y nada más: sin contar signos ni emojis, la tiene y no
    pasa de tres palabras («SCALA», «Scala!», «quiero scala»)."""
    plano = "".join(c if c.isalnum() else " " for c in str(texto or "").lower())
    palabras = plano.split()
    return palabra.lower() in palabras and len(palabras) <= 3


def responder_mensajes(cuenta, agenda, estado, ahora):
    """Le manda el link a quien escribió la palabra por mensaje (así llega la respuesta a una
    historia). Sólo conversaciones recientes y el mensaje más nuevo de cada una. Del texto ajeno
    sólo se compara con la palabra; de lo respondido se guarda el id del mensaje y nada más.
    Ningún error de acá cambia el código de salida."""
    scala = agenda.get("scala")
    if not scala or not scala.get("por_mensaje", True):
        return
    propio = cuenta.usuario.lower()
    pendientes = respondidos = fallos = 0
    try:
        conversaciones = cuenta.leer(f"/{cuenta.id}/conversations",
                                     {"platform": "instagram", "fields": "id,updated_time",
                                      "limit": CONVERSACIONES_A_MIRAR}).get("data") or []
        for conversacion in conversaciones:
            try:
                reciente = ahora - instante(conversacion["updated_time"]) < datetime.timedelta(hours=HORAS_DE_CONVERSACION)
            except (KeyError, ValueError, TypeError):
                continue
            if not conversacion.get("id") or not reciente:
                continue
            abierta = cuenta.leer(f"/{conversacion['id']}", {"fields": "messages"})
            mensajes = (abierta.get("messages") or {}).get("data") or []
            nuevo = None
            for mensaje in mensajes:
                try:
                    cuando = instante(mensaje["created_time"])
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
                if mensaje.get("id") and (nuevo is None or cuando > nuevo[0]):
                    nuevo = (cuando, mensaje["id"])
            if nuevo is None:
                continue
            cuando, id_mensaje = nuevo
            if id_mensaje in estado.respondidos or ahora - cuando >= datetime.timedelta(hours=HORAS_DE_MENSAJE):
                continue
            detalle = cuenta.leer(f"/{id_mensaje}", {"fields": "id,created_time,from,to,message"})
            quien = detalle.get("from")
            if not isinstance(quien, dict) or not quien.get("id"):
                continue
            if (propio and (quien.get("username") or "").lower() == propio) or str(quien["id"]) == str(cuenta.id):
                continue
            if not solo_la_palabra(detalle.get("message"), scala["palabra"]):
                continue
            pendientes += 1
            if respondidos >= RESPUESTAS_POR_CORRIDA:
                continue
            try:
                cuenta.mandar(f"/{cuenta.id}/messages",
                              {"recipient": {"id": quien["id"]},
                               "message": {"text": scala["mensaje"]}}, como_json=True)
            except ErrorDeApi as error:
                if error.codigo in SIN_PERMISO or error.codigo in LIMITE_DE_LLAMADAS or error.codigo in TOKEN_INVALIDO:
                    raise
                fallos += 1
                if fallos >= 3:
                    print("SCALA por mensaje: tres respuestas seguidas fallaron; se sigue en la próxima corrida.")
                    return
                continue
            respondidos += 1
            estado.respondidos.append(id_mensaje)
            estado.guardar()
    except ErrorDeApi as error:
        if error.codigo in SIN_PERMISO:
            print("SCALA por mensaje: Instagram negó el permiso para leer o responder mensajes "
                  "(instagram_business_manage_messages). No se responde nada.")
            return
        if error.codigo in LIMITE_DE_LLAMADAS:
            print("SCALA por mensaje: Instagram pidió esperar; se sigue en la próxima corrida.")
            return
        print(f"SCALA por mensaje: no se pudo seguir ({texto_de_error(error, cuenta.token)}).")
    print(f"SCALA por mensaje: {pendientes} para responder, {respondidos} respondidos"
          + (f", {fallos} que no se pudieron" if fallos else "") + ".")


def conectar_o_avisar(token):
    """La cuenta conectada, o (None, error) si Instagram no contestó bien."""
    try:
        return Cuenta(token).conectar(), None
    except (ErrorDeApi, ErrorDePublicacion) as error:
        return None, error


def agenda_cmd(carpeta, ahora_texto, seco):
    agenda = cargar_agenda(carpeta)
    ahora = (instante(ahora_texto) if ahora_texto
             else datetime.datetime.now(datetime.timezone.utc))
    estado = Estado(os.path.join(carpeta, "publicadas.json"))
    if seco:
        return en_seco(agenda, estado, ahora)
    if not buscar_token():
        print("Sin token: no se publica nada")
        return 0
    token = token_o_salir()
    salida = 0
    try:
        tarde_maxima = datetime.timedelta(hours=agenda.get("tarde_maxima_horas", 36))
        candidatas = []
        for pieza in sorted(agenda["piezas"], key=lambda p: instante(p["cuando"])):
            if not pieza.get("ok") or instante(pieza["cuando"]) > ahora:
                continue
            previo = estado.publicadas.get(pieza["id"]) or {}
            if previo.get("estado") == "falló":
                print(f"{pieza['id']}: está en «falló» y no se reintenta.")
            elif previo.get("estado") in ("publicada", "vencida"):
                continue
            elif ahora - instante(pieza["cuando"]) > tarde_maxima:
                estado.publicadas[pieza["id"]] = {
                    "estado": "vencida", "media_id": None, "permalink": None, "cuando_salio": None,
                    "intentos": previo.get("intentos", 0), "error": None}
                print(f"{pieza['id']}: llegó más de {agenda.get('tarde_maxima_horas', 36)} h tarde. "
                      "Se marca vencida y no se publica.")
            else:
                candidatas.append(pieza)
        cuenta, fallo = conectar_o_avisar(token)
        if cuenta is None:
            # Un token vencido también gasta intentos: si no, la corrida sale bien
            # cada 15 minutos, nadie recibe el mail y la pieza no sale nunca.
            token_vencido = isinstance(fallo, ErrorDeApi) and fallo.codigo in TOKEN_INVALIDO
            if token_vencido:
                print(explicar(fallo, token))
            if candidatas:
                pieza = candidatas[0]
                intentos = (estado.publicadas.get(pieza["id"]) or {}).get("intentos", 0) + 1
                registrar_fallo(estado, pieza["id"], intentos, texto_de_error(fallo, token))
                salida = 1 if intentos < INTENTOS_MAXIMOS else 0
            elif not token_vencido:
                print(f"No se pudo conectar con la cuenta ({texto_de_error(fallo, token)}).")
            return salida
        if candidatas:
            resultado = publicar_una(cuenta, agenda, candidatas[0], estado, ahora)
            estado.guardar()
            if resultado == "error":
                salida = 1 if estado.publicadas[candidatas[0]["id"]]["estado"] == "pendiente" else 0
        else:
            print("No hay nada para publicar.")
        responder_scala(cuenta, agenda, estado, ahora)
        responder_mensajes(cuenta, agenda, estado, ahora)
        return salida
    finally:
        estado.guardar()


def publicar_cmd(id_pieza, carpeta):
    agenda = cargar_agenda(carpeta)
    pieza = pieza_de(agenda, id_pieza)
    if not pieza.get("ok"):
        salir(f"{id_pieza} no tiene su OK en la agenda.", 1)
    estado = Estado(os.path.join(carpeta, "publicadas.json"))
    if (estado.publicadas.get(id_pieza) or {}).get("estado") == "publicada":
        salir(f"{id_pieza} ya está publicada.", 1)
    token = token_o_salir()
    cuenta, fallo = conectar_o_avisar(token)
    if cuenta is None:
        salir(explicar(fallo, token) if isinstance(fallo, ErrorDeApi) else limpio(fallo, token), 1)
    try:
        resultado = publicar_una(cuenta, agenda, pieza, estado, datetime.datetime.now(datetime.timezone.utc))
    finally:
        estado.guardar()
    return 1 if resultado == "error" else 0


def probar_cmd(id_pieza, carpeta):
    agenda = cargar_agenda(carpeta)
    pieza = pieza_de(agenda, id_pieza)
    token = token_o_salir()
    cuenta, fallo = conectar_o_avisar(token)
    if cuenta is None:
        salir(explicar(fallo, token) if isinstance(fallo, ErrorDeApi) else limpio(fallo, token), 1)
    print(f"Probando {id_pieza} ({pieza['tipo']}) sin publicar:")
    try:
        armar_contenedor(cuenta, pieza, agenda["base"], avisar=lambda texto: print(f"  {texto}"))
    except (ErrorDeApi, ErrorDePublicacion) as error:
        print(f"  No anduvo: {texto_de_error(error, token)}")
        return 1
    print("Listo: Instagram bajó los archivos. No se publicó nada (el contenedor vence solo en 24 h).")
    return 0


def subir_token_cmd(repo):
    if not re.fullmatch(r"[A-Za-z0-9][\w.-]*/[\w.-]+", repo):
        salir("El repo va como dueño/nombre.")
    token = token_o_salir()
    try:
        # El token viaja por stdin: en un argumento se vería en la lista de procesos.
        resultado = subprocess.run(["gh", "secret", "set", "IG_ACCESS_TOKEN", "--repo", repo],
                                   input=token, text=True, capture_output=True)
    except OSError:
        salir("No encuentro gh. Instalalo (brew install gh) y entrá con «gh auth login».", 1)
    if resultado.returncode != 0:
        salir("gh no pudo guardar el token: " + limpio((resultado.stderr or "").strip() or "sin detalle", token), 1)
    print(f"Token guardado en {repo}")
    return 0


# --- Preparar el repo público -----------------------------------------------

def caption_de(brief):
    """El primer bloque ``` después de la línea «- **Caption**», sin tocar una letra."""
    with open(brief, encoding="utf-8") as fh:
        lineas = fh.read().splitlines()
    for i, linea in enumerate(lineas):
        if linea.startswith("- **Caption**"):
            break
    else:
        return None
    adentro = None
    for linea in lineas[i + 1:]:
        if linea.strip().startswith("```"):
            if adentro is None:
                adentro = []
                continue
            return "\n".join(adentro)
        if adentro is not None:
            adentro.append(linea)
    return None


def filas_de_lista(path):
    """id -> fecha, de las filas de la tabla «| 2026-10-13 | `hoy-01` … |»."""
    filas = {}
    with open(path, encoding="utf-8") as fh:
        for linea in fh:
            coincide = re.match(r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*`([^`]+)`", linea)
            if coincide:
                filas.setdefault(coincide.group(2), []).append(coincide.group(1))
    return filas


def revisar_caption(caption):
    problemas = []
    if len(caption) > 2200:
        problemas.append(f"el caption tiene {len(caption)} caracteres (máximo 2200)")
    if len(re.findall(r"#\w+", caption)) > 30:
        problemas.append("el caption tiene más de 30 hashtags")
    if len(re.findall(r"@\w+", caption)) > 20:
        problemas.append("el caption tiene más de 20 menciones")
    return problemas


def preparar_cmd(destino):
    destino = os.path.abspath(destino)
    real = os.path.realpath(destino)
    if not os.path.isdir(destino):
        salir(f"{destino} no existe. Clonalo antes (es el repo público).", 1)
    propias = [os.path.realpath(RAIZ), os.path.realpath(os.path.join(RAIZ, "instagram"))]
    if real == propias[0] or real == propias[1] or real.startswith(propias[1] + os.sep):
        salir("El destino es esta misma carpeta: tiene que ser el clon del repo público.", 1)
    for necesario in (os.path.join(LANZAMIENTO, "publicacion.json"), os.path.join(LANZAMIENTO, "lista.md"),
                      os.path.join(PUBLICO, "publicar.yml"), os.path.join(PUBLICO, "README.md")):
        if not os.path.exists(necesario):
            salir(f"Falta {os.path.relpath(necesario, RAIZ)}.", 1)
    with open(os.path.join(LANZAMIENTO, "publicacion.json"), encoding="utf-8") as fh:
        config = json.load(fh)
    filas = filas_de_lista(os.path.join(LANZAMIENTO, "lista.md"))

    piezas, copias, problemas = [], {}, []
    for id_pieza, ok in (config.get("ok") or {}).items():
        if not ok:
            continue
        carpeta = os.path.join(LANZAMIENTO, "piezas", id_pieza)
        errores = []
        if len(filas.get(id_pieza, [])) != 1:
            errores.append("tiene que estar en una sola fila con fecha de lista.md")
        pieza_json = os.path.join(carpeta, "final", "pieza.json")
        datos = {}
        if os.path.exists(pieza_json):
            with open(pieza_json, encoding="utf-8") as fh:
                datos = json.load(fh)
        else:
            errores.append("falta final/pieza.json")
        caption = caption_de(os.path.join(carpeta, "brief.md")) if os.path.exists(os.path.join(carpeta, "brief.md")) else None
        if caption is None:
            errores.append("no encuentro el caption en brief.md")
        else:
            errores += revisar_caption(caption)
        final = os.path.join(carpeta, "final")
        tipo, archivos, tapa = datos.get("formato"), [], None
        if tipo == "reel":
            archivos, tapa = ["reel.mp4"], "tapa.jpg"
        elif tipo == "carrusel":
            archivos = sorted(n for n in (os.listdir(final) if os.path.isdir(final) else [])
                              if re.fullmatch(r"\d\d\.jpg", n))
            if not 2 <= len(archivos) <= 10:
                errores.append(f"un carrusel lleva de 2 a 10 imágenes NN.jpg y hay {len(archivos)}")
        elif datos:
            errores.append(f"el formato «{tipo}» no se sabe publicar")
        for archivo in archivos + ([tapa] if tapa else []):
            if not os.path.exists(os.path.join(final, archivo)):
                errores.append(f"falta final/{archivo}")
        if len(datos.get("alt") or "") > 1000:
            errores.append("el texto alternativo pasa de 1000 caracteres")
        if errores:
            problemas += [f"{id_pieza}: {e}" for e in errores]
            continue
        piezas.append({"id": id_pieza, "cuando": f"{filas[id_pieza][0]}T{config['hora']}:00{config['zona']}",
                       "tipo": tipo, "archivos": archivos, "tapa": tapa, "caption": caption,
                       "alt": datos.get("alt") or "", "ok": ok})
        copias[id_pieza] = [os.path.join(final, a) for a in archivos + ([tapa] if tapa else [])]
    vistos = {}
    for pieza in piezas:
        if pieza["caption"] in vistos:
            problemas.append(f"{pieza['id']}: tiene el mismo caption que {vistos[pieza['caption']]}")
        vistos[pieza["caption"]] = pieza["id"]
    if problemas:
        print("No se armó nada:", file=sys.stderr)
        for problema in problemas:
            print(f"  {problema}", file=sys.stderr)
        sys.exit(1)

    piezas.sort(key=lambda p: (instante(p["cuando"]), p["id"]))
    agenda = {"repo": config["repo"], "base": config["base"].rstrip("/") + "/",
              "tarde_maxima_horas": config["tarde_maxima_horas"], "scala": config["scala"], "piezas": piezas}

    raiz_piezas = os.path.join(destino, "piezas")
    os.makedirs(raiz_piezas, exist_ok=True)
    ids = {p["id"] for p in piezas}
    for nombre in os.listdir(raiz_piezas):
        if nombre not in ids and os.path.isdir(os.path.join(raiz_piezas, nombre)):
            shutil.rmtree(os.path.join(raiz_piezas, nombre))
    for pieza in piezas:
        carpeta = os.path.join(raiz_piezas, pieza["id"])
        os.makedirs(carpeta, exist_ok=True)
        subir = {os.path.basename(origen) for origen in copias[pieza["id"]]}
        for sobrante in set(os.listdir(carpeta)) - subir:
            sobrante = os.path.join(carpeta, sobrante)
            shutil.rmtree(sobrante) if os.path.isdir(sobrante) else os.remove(sobrante)
        for origen in copias[pieza["id"]]:
            shutil.copyfile(origen, os.path.join(carpeta, os.path.basename(origen)))
    escribir_atomico(os.path.join(destino, "agenda.json"), como_json(agenda))
    if os.path.abspath(__file__) != os.path.join(destino, "ig.py"):
        shutil.copyfile(os.path.abspath(__file__), os.path.join(destino, "ig.py"))
    os.makedirs(os.path.join(destino, ".github", "workflows"), exist_ok=True)
    shutil.copyfile(os.path.join(PUBLICO, "publicar.yml"), os.path.join(destino, ".github", "workflows", "publicar.yml"))
    shutil.copyfile(os.path.join(PUBLICO, "README.md"), os.path.join(destino, "README.md"))
    open(os.path.join(destino, ".nojekyll"), "a").close()
    if not os.path.exists(os.path.join(destino, "publicadas.json")):
        escribir_atomico(os.path.join(destino, "publicadas.json"), como_json({"publicadas": {}, "respondidos": []}))

    print(f"{'id':<17} {'cuándo':<25} {'tipo':<9} {'archivos':>8} {'caption':>8}")
    for pieza in piezas:
        cantidad = len(pieza["archivos"]) + (1 if pieza["tapa"] else 0)
        print(f"{pieza['id']:<17} {pieza['cuando']:<25} {pieza['tipo']:<9} {cantidad:>8} {len(pieza['caption']):>8}")
    print(f"\n{len(piezas)} piezas armadas en {destino}")
    return 0


def main(argv=None):
    analizador = argparse.ArgumentParser(description="Instagram de Scala, por la API oficial.")
    comandos = analizador.add_subparsers(dest="comando", required=True)
    comandos.add_parser("verificar")
    comandos.add_parser("refrescar")
    preparar = comandos.add_parser("preparar")
    preparar.add_argument("--destino", required=True)
    agenda = comandos.add_parser("agenda")
    agenda.add_argument("--carpeta", default=".")
    agenda.add_argument("--ahora")
    agenda.add_argument("--en-seco", action="store_true", dest="en_seco")
    publicar = comandos.add_parser("publicar")
    publicar.add_argument("id")
    publicar.add_argument("--carpeta", default=".")
    probar = comandos.add_parser("probar")
    probar.add_argument("id")
    probar.add_argument("--carpeta", default=".")
    subir = comandos.add_parser("subir-token")
    subir.add_argument("--repo", required=True)
    args = analizador.parse_args(argv)
    if args.comando == "verificar":
        sys.exit(verificar())
    if args.comando == "refrescar":
        sys.exit(refrescar())
    if args.comando == "preparar":
        sys.exit(preparar_cmd(args.destino))
    if args.comando == "agenda":
        sys.exit(agenda_cmd(args.carpeta, args.ahora, args.en_seco))
    if args.comando == "publicar":
        sys.exit(publicar_cmd(args.id, args.carpeta))
    if args.comando == "probar":
        sys.exit(probar_cmd(args.id, args.carpeta))
    sys.exit(subir_token_cmd(args.repo))


if __name__ == "__main__":
    main()
