"""Bot de Telegram por long polling. Reemplaza al listener de n8n (telegramRepliesBot01, ahora
desactivado). Implementa la conversación completa de un borrador: antes de confirmar, genera 3
propuestas de reescritura por IA (Gemini) para que el usuario elija (o escriba directamente su
propio texto), confirma y "publica" (simulado -- todavía no hay integración real con X).

A diferencia del prototipo del que sale esta funcionalidad (`x-prototipo-confirmar`), acá se
mantiene el patrón de edición en el lugar que ya traía este archivo desde la Etapa B: el mismo
aviso de tweet nuevo se transforma, editándolo, en la tarjeta de confirmación (ver
ConversacionTelegram.avanzar_a_esperando_texto/procesar_texto_respuesta) -- no se migró al patrón
"ningún mensaje se edita nunca" del prototipo porque producción nunca funcionó así y ese es un
cambio de comportamiento visible para el usuario (queda o no un historial completo en el chat), no
una mejora puramente técnica. Ver README para el detalle de esta decisión.

Corre como servicio aparte del Flask de /check y /borradores (ver docker-compose.yml): así un
colgazo de twscrape no frena las respuestas de Telegram, y viceversa. Comparte estado.db con ese
otro servicio (bot_x.py) por bind mount en modo WAL, a través del módulo estado_db.py.

Organización del módulo (ver README para el detalle de cada clase):
- ClienteGemini: genera las 3 propuestas de reescritura por IA.
- DetectorTweetsNuevos: chequeo periódico de tweets nuevos y primer aviso a Telegram.
- ConversacionTelegram: máquina de estados de elegir/editar/confirmar/publicar un borrador.
- main(): arma la Application de python-telegram-bot y conecta las piezas de arriba.
"""
import asyncio
import hashlib
import html
import json
import logging
import os

import httpx
from google import genai
from google.genai import types as genai_types
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from estado_db import EstadoDB


def _leer_chat_id():
    valor = os.environ.get('CHAT_ID')
    if valor is None or valor.strip() == '':
        raise RuntimeError(
            "Falta la variable de entorno CHAT_ID (id del chat de Telegram al que responde "
            "el bot). Definila en .env."
        )
    try:
        return int(valor)
    except ValueError:
        raise RuntimeError(f"CHAT_ID inválido en el entorno: {valor!r} (debe ser un entero)") from None


CHAT_ID = _leer_chat_id()

MAX_CARACTERES = 280

# 'real' (default) publica de verdad en X al tocar "Publicar" -- ver ConversacionTelegram.manejar_pub.
# 'mock' deja el comportamiento simulado de siempre (registrar_publicacion), sin pegarle a X ni a
# bot-tweets para nada de esto -- pensado solo para iterar sobre la conversación sin gastar
# tweets reales, no para uso normal.
MODO_PUBLICACION = os.environ.get('MODO_PUBLICACION', 'real').strip().lower()

logging.basicConfig(
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    level=logging.INFO,
)
logging.getLogger('httpx').setLevel(logging.WARNING)  # si no, una línea de log por cada long-poll.
# El SDK de Gemini larga un warning informativo de "automatic function calling" en la primera
# llamada aunque no usemos tools -- no aporta nada acá, así que se silencia igual que httpx.
logging.getLogger('google_genai.models').setLevel(logging.ERROR)
logger = logging.getLogger('bot_telegram')


# --- Textos de la conversación (sin estado propio: solo arman el texto que se manda/edita) ---

TEXTO_ESPERANDO_ELECCION = (
    "Mirá el mensaje de abajo con las propuestas y respondé ahí "
    "(1, 2 o 3, o tu propio texto)."
)

TEXTO_ESPERANDO_TEXTO_MANUAL = "Mirá el mensaje de abajo y respondé con el texto del tweet."


def texto_esperando_eleccion(opciones):
    return TEXTO_ESPERANDO_ELECCION if opciones else TEXTO_ESPERANDO_TEXTO_MANUAL

TEXTO_PEDIR_TEXTO_VACIO = (
    "Llegó vacío. Respondé de nuevo con el texto del tweet (o con 1, 2 o 3 para elegir una de "
    "las propuestas de más arriba)."
)


def texto_pedir_texto_largo(largo):
    return (
        f"Tiene {largo} caracteres y el máximo son {MAX_CARACTERES}. Achicalo y respondé de "
        "nuevo (o respondé 1, 2 o 3 para elegir una de las propuestas de más arriba)."
    )


def texto_opciones(tweet_texto, opciones):
    numeradas = '\n\n'.join(f'{i + 1}️⃣ {texto}' for i, texto in enumerate(opciones))
    return (
        f"Propuestas de reescritura para:\n«{tweet_texto}»\n\n{numeradas}\n\n"
        "Respondé con 1, 2 o 3 para elegir esa propuesta, o escribí directamente tu propio "
        f"texto (máx. {MAX_CARACTERES} caracteres)."
    )


def texto_pedir_texto_manual(tweet_texto):
    """Reemplaza a texto_opciones() cuando no hay propuestas de IA (Gemini falló o se quedó sin
    cuota -- ver ClienteGemini.generar_propuestas y procesar_tw). Mismo lugar en la conversación
    (ForceReply pidiendo el texto final), pero sin las 3 opciones numeradas porque no hay
    ninguna: se avisa el motivo así no parece un mensaje roto o incompleto."""
    return (
        "⚠️ No se pudieron generar propuestas con IA (falló Gemini, o se agotó la cuota gratuita "
        "de hoy).\n\n"
        f"Escribí directamente el texto del tweet (máx. {MAX_CARACTERES} caracteres) para:\n"
        f"«{tweet_texto}»"
    )


def texto_confirmacion(texto_final):
    pregunta = "¿Publicamos? (simulado — no se manda a X)" if MODO_PUBLICACION == 'mock' else "¿Publicamos?"
    return f"📝 Texto final:\n\n{texto_final}\n\n{pregunta}"


TEXTO_PUBLICANDO = "Publicando (simulado)... 🐦"
TEXTO_PUBLICANDO_REAL = "Publicando... 🐦"


def texto_publicado(texto_final):
    return (
        "✅ Publicado (simulado) — esto no se mandó a X todavía, "
        "es para probar el flujo.\n\n"
        f"{texto_final}"
    )


def texto_publicado_real(texto_final, url):
    return f"✅ Publicado en X:\n\n{texto_final}\n\n{url}"


def texto_error_publicacion(detalle):
    return f"❌ No se pudo publicar: {detalle}\n\nPodés tocar Publicar de nuevo."


TEXTO_REVISION_MANUAL = (
    "⚠️ No se pudo confirmar si el tweet salió publicado o no. Este borrador queda pausado -- "
    "revisá el perfil de X a mano antes de decidir qué hacer (no se reintenta solo, para no "
    "arriesgar una publicación duplicada)."
)

TEXTO_DESCARTADO = "🗑️ Descartado, no se publica nada."
TEXTO_YA_PROCESADO = "Ya se procesó — mirá la tarjeta de arriba."
TEXTO_TEXTO_CAMBIO = "El texto cambió, mirá la tarjeta de nuevo antes de publicar."
TEXTO_BOTON_VENCIDO = "Ese botón ya no está — mirá el mensaje de abajo 👆"
TEXTO_ERROR_GENERICO = "Hubo un error, probá de nuevo en un rato."


class ClienteGemini:
    """Genera las 3 propuestas de reescritura por IA (Gemini) de un tweet. Migrado del prototipo
    x-prototipo-confirmar, probado ahí contra la API real.

    generar_propuestas() es el único punto de entrada que usa el resto del código: si no hay
    GEMINI_API_KEY configurada, devuelve propuestas de prueba (stub) en vez de llamar a la IA
    real, para poder seguir probando el resto de la conversación sin la key cargada.
    """

    # Reescribir un tweet en 3 variantes no necesita el modelo más potente: la línea "flash"
    # alcanza de sobra y tiene un nivel gratis generoso para un volumen de uso que puede ser
    # frecuente.
    #
    # El id se reconfirmó contra la API real al traer esto a producción (13/09, no se asumió que
    # siguiera vigente por estar en el prototipo): 'gemini-2.5-flash' -- el nombre "obvio" de un
    # flash estable -- sigue devolviendo HTTP 404 con el mensaje "This model
    # models/gemini-2.5-flash is no longer available to new users. Please update your code to use
    # models/gemini-3.6-flash for the latest features", y una llamada real con este id sigue
    # funcionando. Se usa el id sin el prefijo 'models/' porque el SDK acepta ambas formas.
    MODELO_IA = 'gemini-3.6-flash'

    # Timeout explícito para la llamada HTTP a Gemini -- sin esto, el SDK de google-genai 2.23.0
    # pasa timeout=None EXPLÍCITO a httpx (confirmado leyendo el código fuente instalado:
    # _api_client.py::get_timeout_in_seconds() devuelve None si HttpOptions.timeout no está
    # seteado, y ese None llega tal cual a self._async_httpx_client.request(...,
    # timeout=http_request.timeout)). En httpx, un timeout=None explícito no es "usar el default
    # del cliente", es *sin límite en absoluto*. Como el bot corre con max_concurrent_updates=1
    # (default de python-telegram-bot, sin cambiar acá), un cuelgue real de esta llamada (no un
    # error rápido -- eso ya lo atrapa el except Exception de ConversacionTelegram.procesar_tw)
    # congela el bot ENTERO: ningún otro borrador, botón ni mensaje se procesa hasta que se
    # resuelva. Se setea a nivel de Client (ver _obtener_cliente) en vez de por llamada porque
    # _build_request usa self._http_options tal cual cuando no se pasa un http_options por
    # request, así que alcanza con configurarlo una sola vez acá.
    TIMEOUT_SDK_MS = 15_000  # HttpOptions.timeout se expresa en milisegundos, no segundos.

    # Red de seguridad adicional además del timeout del SDK: cubre un cuelgue en una etapa
    # anterior a la request HTTP en sí (por ejemplo, resolución de DNS o el propio setup interno
    # del cliente), donde TIMEOUT_SDK_MS no aplicaría. generar_propuestas_ia puede hacer hasta 2
    # llamadas HTTP (la original más 1 reintento si alguna propuesta se pasa de MAX_CARACTERES --
    # ver ahí), así que este total deja margen para las dos (2 x 15 s) más colchón, en vez de
    # calcularlo ajustado al límite.
    TIMEOUT_TOTAL_IA_SEGUNDOS = 35

    # El schema de Gemini (response_schema) es un subconjunto de OpenAPI 3.0: 'additionalProperties'
    # no existe ahí -- se probó contra la API real y devuelve 400 ('Unknown name
    # "additional_properties"... Cannot find field'), a diferencia del json_schema completo que
    # aceptaba Anthropic. La cantidad exacta de propuestas (3) se sigue validando en código en vez
    # de con minItems/maxItems, mismo criterio defensivo que ya usaba esto con Anthropic.
    ESQUEMA_PROPUESTAS = {
        'type': 'object',
        'properties': {
            'propuestas': {
                'type': 'array',
                'items': {'type': 'string'},
            },
        },
        'required': ['propuestas'],
    }

    def __init__(self):
        # Se crea perezosamente en _obtener_cliente (no acá) porque instanciar genai.Client exige
        # GEMINI_API_KEY -- si se creara en __init__, el modo stub (sin key configurada) rompería
        # apenas se instancia ClienteGemini, incluso sin llegar a usar la IA real.
        self._cliente = None

    def _obtener_cliente(self):
        """Devuelve la única instancia de genai.Client de este proceso, creándola en el primer
        uso real -- evita armar de nuevo el httpx.AsyncClient interno del SDK por cada tweet."""
        if self._cliente is None:
            self._cliente = genai.Client(
                api_key=os.environ['GEMINI_API_KEY'],
                # Ver TIMEOUT_SDK_MS más arriba. Al no pasarse un http_options por request en
                # _pedir_propuestas_a_gemini, _build_request usa este de acá (self._http_options)
                # sin modificar -- confirmado leyendo _api_client.py::_build_request.
                http_options=genai_types.HttpOptions(timeout=self.TIMEOUT_SDK_MS),
            )
        return self._cliente

    @staticmethod
    def _prompt_reescritura(tweet_texto):
        return (
            "Reescribí el siguiente tweet manteniendo el mismo sentido. Necesito 3 alternativas "
            "distintas entre sí, con un tono similar al original y un largo similar, cada una de "
            "hasta 280 caracteres. No inventes información que no esté en el tweet original.\n\n"
            f"Tweet original:\n{tweet_texto}"
        )

    @staticmethod
    def _propuestas_stub(tweet_texto):
        """Propuestas fijas para probar el resto del flujo sin llamar a la IA real -- ver
        generar_propuestas() para cuándo se usa esto en vez de generar_propuestas_ia()."""
        return [f"[PROPUESTA DE PRUEBA {n} — sin IA real todavía] {tweet_texto}" for n in (1, 2, 3)]

    @staticmethod
    def _recortar_a_280(texto):
        """Último recurso si ni el prompt ni un reintento lograron que Gemini devuelva una
        propuesta de como mucho MAX_CARACTERES (ver generar_propuestas_ia): corta en el último
        espacio antes del límite para no partir una palabra al medio (si no hay espacio útil,
        corta seco). No se agrega "..." ni nada al final -- ocuparía caracteres extra y hay que
        seguir cumpliendo el límite exacto que exige X.
        """
        if len(texto) <= MAX_CARACTERES:
            return texto
        corte = texto.rfind(' ', 0, MAX_CARACTERES)
        return texto[:corte] if corte > 0 else texto[:MAX_CARACTERES]

    async def _pedir_propuestas_a_gemini(self, tweet_texto):
        """Una llamada a la API de Gemini pidiendo las 3 propuestas. Separado de
        generar_propuestas_ia para poder reintentar una vez sin duplicar la lógica de reintento
        entera (ver ahí)."""
        cliente = self._obtener_cliente()
        respuesta = await cliente.aio.models.generate_content(
            model=self.MODELO_IA,
            contents=self._prompt_reescritura(tweet_texto),
            config=genai_types.GenerateContentConfig(
                response_mime_type='application/json',
                response_schema=self.ESQUEMA_PROPUESTAS,
                # thinking_level MINIMAL: reescribir un tweet no necesita razonamiento profundo, y
                # de paso baja bastante la latencia y el costo -- probado contra la API real, sin
                # esto el modelo gasta ~500 tokens de "thinking" de más por request para esta tarea.
                thinking_config=genai_types.ThinkingConfig(thinking_level=genai_types.ThinkingLevel.MINIMAL),
            ),
        )

        if not respuesta.candidates:
            motivo = respuesta.prompt_feedback.block_reason if respuesta.prompt_feedback else None
            raise RuntimeError(f'Gemini no devolvió candidatos (prompt bloqueado, motivo={motivo})')

        finish_reason = respuesta.candidates[0].finish_reason
        if finish_reason != genai_types.FinishReason.STOP:
            # Equivalente al chequeo de stop_reason=='refusal' que había con Anthropic: cualquier
            # corte que no sea STOP (SAFETY, PROHIBITED_CONTENT, MAX_TOKENS, etc.) es una
            # respuesta que no hay que usar.
            raise RuntimeError(f'Gemini terminó con finish_reason={finish_reason!r} en vez de STOP')

        propuestas = json.loads(respuesta.text)['propuestas']
        if not isinstance(propuestas, list) or len(propuestas) != 3:
            raise ValueError(f"la IA devolvió propuestas={propuestas!r} en vez de una lista de 3")
        return [str(p) for p in propuestas]

    async def generar_propuestas_ia(self, tweet_texto):
        """Le pide a la API de Gemini 3 reescrituras del tweet original. Devuelve una lista de 3
        strings, cada uno de como mucho MAX_CARACTERES. Ante cualquier falla (red, rate limit,
        JSON inesperado, bloqueo de contenido) deja que la excepción suba -- el llamador
        (ConversacionTelegram.procesar_tw) decide qué hacer: loguear, avisar y dejar el borrador
        como estaba, igual que ya hace con cualquier otra llamada externa que puede fallar.

        El prompt ya pide <=280 caracteres, pero no hay forma de forzarlo por schema, así que
        puede pasar que alguna propuesta se pase. Criterio elegido: si pasa, se descarta la tanda
        entera y se pide una vez más (mismo prompt) -- lo más probable es que en el reintento
        entre bien. Si el reintento también se pasa, se recorta esa propuesta puntual con
        _recortar_a_280() en vez de reintentar sin límite: nunca hay que dejar al usuario sin
        propuestas ni exponerle un texto que va a rebotar en X por largo.
        """
        propuestas = await self._pedir_propuestas_a_gemini(tweet_texto)

        if any(len(p) > MAX_CARACTERES for p in propuestas):
            logger.warning(
                "Gemini devolvió una propuesta de más de %s caracteres, reintentando una vez",
                MAX_CARACTERES,
            )
            propuestas = await self._pedir_propuestas_a_gemini(tweet_texto)

        return [self._recortar_a_280(p) for p in propuestas]

    async def generar_propuestas(self, tweet_texto):
        """Punto de entrada único para conseguir las 3 propuestas: si hay GEMINI_API_KEY, llama a
        la IA real; si no, devuelve propuestas de prueba bien marcadas para poder seguir probando
        el resto de la conversación mientras la key todavía no está cargada. En cuanto la key
        aparezca en el .env, este mismo código empieza a llamar a la API real sin tocar nada más.
        """
        if not os.environ.get('GEMINI_API_KEY'):
            logger.warning(
                "GEMINI_API_KEY no está configurada -- devolviendo propuestas de prueba (stub) "
                "en vez de llamar a la IA real."
            )
            return self._propuestas_stub(tweet_texto)
        return await self.generar_propuestas_ia(tweet_texto)


class DetectorTweetsNuevos:
    """Detección de tweets nuevos + primer aviso a Telegram (migrado de n8n, ver plan de
    migración). chequeo_periodico() es el job de job_queue.run_repeating que reemplaza al
    Schedule Trigger + el resto del workflow de n8n hasta "Guardar borrador" inclusive:
    consulta /check en bot-tweets, arma los avisos de los tweets nuevos de cada cuenta y avanza
    el since_id guardado en estado.db a medida que cada aviso se manda bien.
    """

    # Nombre del servicio bot-tweets dentro de la red de docker compose (no host.docker.internal:
    # ahí es donde n8n, que corría FUERA de este compose, alcanzaba a bot-tweets vía el puerto
    # publicado en el host; acá los dos servicios están en la misma red de compose y se resuelven
    # por nombre de servicio).
    BOT_TWEETS_URL = os.environ.get('BOT_TWEETS_URL', 'http://bot-tweets:5000')

    INTERVALO_CHEQUEO_SEGUNDOS = 180  # mismo intervalo que el Schedule Trigger de n8n (cada 3 min).
    TIMEOUT_CHECK_SEGUNDOS = 60  # igual que el timeout del nodo HTTP Request a /check en n8n.
    TIMEOUT_BORRADOR_SEGUNDOS = 4  # igual que el timeout del nodo "Guardar borrador" en n8n.

    MAX_TEXTO_AVISO = 3800  # deja margen debajo del límite de 4096 caracteres de un mensaje Telegram.

    NOTA_INCOMPLETO = (
        '⚠️ Hubo más tweets nuevos de los que se pudieron traer: puede haber anteriores a este sin '
        'avisar.\n\n'
    )

    def __init__(self, estado_db, chat_id):
        self.estado_db = estado_db
        self.chat_id = chat_id

    @classmethod
    def recortar_texto_aviso(cls, texto):
        """Recorta `texto` a MAX_TEXTO_AVISO caracteres. Equivalente al 'recortar' del nodo de
        n8n que este código reemplaza, pero sin la guarda de pares subrogados que tenía ese
        código JS: esa guarda existía porque JavaScript indexa strings en unidades UTF-16 (un
        emoji fuera del BMP ocupa dos, y cortar justo en el medio partía el emoji en dos
        caracteres inválidos). Python indexa strings por code point, no por unidad UTF-16, así
        que un corte por índice acá nunca cae en la mitad de un emoji de un solo code point -- no
        hace falta la guarda."""
        if len(texto) <= cls.MAX_TEXTO_AVISO:
            return texto
        return texto[:cls.MAX_TEXTO_AVISO] + '…'

    @classmethod
    def armar_mensaje_aviso(cls, cuenta, texto, nota=''):
        """El texto del aviso, con HTML escapado para mandarlo con parse_mode='HTML' -- usa
        html.escape de la stdlib (arreglo de Palito) en vez del reemplazo manual de &/</> que
        hacía el nodo de n8n, que corría el riesgo de quedar incompleto si Telegram alguna vez
        empieza a interpretar alguna otra entidad."""
        return f"{nota}🐦 Nuevo tweet de @{html.escape(cuenta)}  {html.escape(cls.recortar_texto_aviso(texto))}"

    @classmethod
    def armar_items_nuevos(cls, cuenta, resultado, since_id_actual):
        """A partir de la respuesta de /check para UNA cuenta, arma la lista de tweets a avisar
        (del más viejo al más nuevo) y si esta tanda es línea base para esa cuenta -- mismo
        criterio que tenía el nodo "Code in JavaScript" de n8n que este código reemplaza: sin
        since_id guardado todavía, la tanda entera es línea base y se guarda el tweet más nuevo
        sin avisar nada.

        Devuelve (items, es_linea_base). Cada item trae cuenta/id/texto/imagen/fecha/mensaje,
        listo para pasar a avisar_tweet_nuevo(). Levanta RuntimeError si `resultado` viene con
        error o mal formado -- el llamador decide qué hacer con esa cuenta puntual sin frenar a
        las demás (mismo criterio que el nodo de n8n, que acumulaba errores por cuenta en vez de
        cortar todo)."""
        if not isinstance(resultado, dict) or resultado.get('error'):
            raise RuntimeError(resultado.get('error', 'respuesta vacía') if isinstance(resultado, dict) else 'respuesta vacía')
        tweets = resultado.get('tweets')
        if not isinstance(tweets, list):
            raise RuntimeError('respuesta sin lista de tweets')
        for tweet in tweets:
            id_tweet = tweet.get('id') if isinstance(tweet, dict) else None
            if not (isinstance(id_tweet, str) and id_tweet.isdigit()):
                raise RuntimeError(f'id inválido ({id_tweet!r})')

        es_linea_base = since_id_actual is None
        ordenados = sorted(tweets, key=lambda t: int(t['id']))
        if es_linea_base:
            ordenados = ordenados[-1:]

        items = []
        for i, tweet in enumerate(ordenados):
            texto = tweet.get('texto') or ''
            # El bot no llegó hasta el último guardado: se avisa en el primer mensaje de la tanda.
            nota = cls.NOTA_INCOMPLETO if (resultado.get('incompleto') and not es_linea_base and i == 0) else ''
            items.append({
                'cuenta': cuenta,
                'id': tweet['id'],
                'texto': texto,
                'imagen': tweet.get('imagen'),
                'fecha': tweet.get('fecha') or '',
                'mensaje': cls.armar_mensaje_aviso(cuenta, texto, nota),
            })
        return items, es_linea_base

    async def pedir_check(self, client, since_ids):
        """POST /check contra bot-tweets con los since_id actuales de todas las cuentas
        conocidas. Una sola llamada para todas las cuentas (igual que hacía el HTTP Request de
        n8n): bot_x.py ya espacía las consultas a X con sleep(2) entre cuenta y cuenta, así que
        pedirlas juntas reparte mejor la carga contra el rate limit que un pedido HTTP por
        cuenta."""
        respuesta = await client.post(
            f'{self.BOT_TWEETS_URL}/check', json=since_ids, timeout=self.TIMEOUT_CHECK_SEGUNDOS,
        )
        respuesta.raise_for_status()
        return respuesta.json()

    async def crear_borrador_remoto(self, client, item):
        """POST /borradores contra bot-tweets: crea (o recupera, si ya existía por tweet_id --
        ver EstadoDB.crear_borrador, idempotente) el borrador para este tweet y devuelve su id
        real. Se llama ANTES de mandar el aviso a Telegram -- ver avisar_tweet_nuevo() para por
        qué ese orden es justamente el arreglo del bug real de producción."""
        respuesta = await client.post(
            f'{self.BOT_TWEETS_URL}/borradores',
            json={
                'tweet_id': item['id'], 'cuenta': item['cuenta'],
                'texto': item['texto'], 'fecha': item['fecha'],
            },
            timeout=self.TIMEOUT_BORRADOR_SEGUNDOS,
        )
        respuesta.raise_for_status()
        return respuesta.json()['id']

    async def avisar_tweet_nuevo(self, context, client, item):
        """Implementa el paso 2 del plan de migración, en el orden que arregla el bug real de
        producción: n8n mandaba el aviso con callback_data=tw:<id de TWEET> antes incluso de
        crear el borrador (el nodo "Guardar borrador" corría después de "Send a text message"), y
        el bot de Python siempre buscó ese id contra la columna `id` del borrador (8 hex), no
        contra `tweet_id` -- así que el botón nunca podía encontrar nada, ni una vez arreglado el
        orden.

        Acá el orden es: (1) crear/recuperar el borrador primero, (2) armar callback_data con el
        id REAL que devuelve /borradores, (3) recién ahí mandar el mensaje. Devuelve True solo si
        el aviso se mandó bien -- el llamador (chequeo_periodico) únicamente avanza el since_id en
        ese caso (ver el comentario ahí sobre el arreglo de Palito)."""
        try:
            id_borrador = await self.crear_borrador_remoto(client, item)
        except Exception:
            logger.exception(
                "No se pudo crear/recuperar el borrador para cuenta=%s tweet_id=%s -- no se manda "
                "el aviso (se reintenta en el próximo chequeo, /borradores es idempotente por "
                "tweet_id así que no hay riesgo de duplicar el borrador)",
                item['cuenta'], item['id'],
            )
            return False

        try:
            await context.bot.send_message(
                chat_id=self.chat_id,
                text=item['mensaje'],
                parse_mode='HTML',
                reply_markup=ConversacionTelegram.teclado_aviso_tw(id_borrador),
            )
        except TelegramError:
            logger.exception(
                "borrador_id=%s ya existe (creado o recuperado) pero no se pudo mandar el aviso "
                "por Telegram para cuenta=%s tweet_id=%s -- se reintenta en el próximo chequeo sin "
                "duplicar el borrador",
                id_borrador, item['cuenta'], item['id'],
            )
            return False

        logger.info(
            "cuenta=%s tweet_id=%s borrador_id=%s resultado=avisado_ok",
            item['cuenta'], item['id'], id_borrador,
        )
        return True

    async def chequeo_periodico(self, context: ContextTypes.DEFAULT_TYPE):
        """Job de job_queue.run_repeating: reemplaza al Schedule Trigger + el resto del workflow
        de n8n hasta "Guardar borrador" inclusive. Corre cada INTERVALO_CHEQUEO_SEGUNDOS."""
        since_ids = self.estado_db.leer_since_ids()

        async with httpx.AsyncClient() as client:
            try:
                data = await self.pedir_check(client, since_ids)
            except Exception:
                logger.exception(
                    "No se pudo llamar a /check en bot-tweets -- se reintenta en el próximo chequeo",
                )
                return

            for cuenta, resultado in data.items():
                try:
                    items, es_linea_base = self.armar_items_nuevos(cuenta, resultado, since_ids.get(cuenta))
                except RuntimeError as error:
                    logger.error("cuenta=%s /check devolvió un resultado inválido: %s", cuenta, error)
                    continue

                if es_linea_base:
                    if items:
                        self.estado_db.actualizar_since_id(cuenta, items[0]['id'])
                        logger.info(
                            "cuenta=%s resultado=linea_base_guardada ultimo_id=%s",
                            cuenta, items[0]['id'],
                        )
                    continue

                for item in items:
                    # items ya viene ordenado del tweet más viejo al más nuevo (armar_items_nuevos).
                    if not await self.avisar_tweet_nuevo(context, client, item):
                        # Arreglo de Palito (Medio): si acá se siguiera con el resto de la tanda,
                        # un tweet más nuevo que sí saliera bien correría el since_id más allá de
                        # este tweet fallido, que quedaría saltado para siempre (el próximo
                        # chequeo ya no lo pediría, porque since_id > su id). Se corta la tanda de
                        # esta cuenta acá: este tweet (y los más nuevos que quedaron atrás) se
                        # reintentan enteros en el próximo chequeo.
                        break
                    self.estado_db.actualizar_since_id(cuenta, item['id'])


class ConversacionTelegram:
    """Máquina de estados de la conversación con el usuario sobre un borrador: elegir o escribir
    el texto final, confirmarlo y publicarlo. Cada borrador recorre los pasos
    nuevo -> esperando_texto -> confirmando -> publicado (o cancelado) -- con MODO_PUBLICACION=mock
    el último paso es publicado_simulado en vez de publicado, y con la publicación real hay además
    dos pasos transitorios (publicando) o de excepción (revision_manual) -- ver manejar_pub.
    Persistido en estado.db a través de EstadoDB.

    on_callback y on_reply son los dos puntos de entrada que python-telegram-bot invoca (ver
    main()): el primero para los botones de las tarjetas, el segundo para las respuestas de texto
    del usuario.
    """

    # Mismo servicio y mismo criterio de nombre que ya usa DetectorTweetsNuevos.BOT_TWEETS_URL
    # (ver ahí) -- se repite acá en vez de compartir un atributo entre las dos clases porque cada
    # una ya define lo que necesita por su cuenta (mismo patrón que el resto de este archivo).
    BOT_TWEETS_URL = os.environ.get('BOT_TWEETS_URL', 'http://bot-tweets:5000')
    TIMEOUT_PUBLICAR_SEGUNDOS = 40  # por encima de ClienteX.TIMEOUT_PUBLICAR (30s) del otro lado.

    def __init__(self, estado_db, cliente_gemini, chat_id):
        self.estado_db = estado_db
        self.cliente_gemini = cliente_gemini
        self.chat_id = chat_id

    @staticmethod
    def calcular_hash6(texto_final):
        return hashlib.sha256(texto_final.encode('utf-8')).hexdigest()[:6]

    def teclado_m4(self, id_borrador, texto_final):
        hash6 = self.calcular_hash6(texto_final)
        return InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ Publicar', callback_data=f'pub:{id_borrador}:{hash6}'),
            InlineKeyboardButton('✏️ Seguir editando', callback_data=f'edit:{id_borrador}'),
            InlineKeyboardButton('🗑️ Descartar', callback_data=f'desc:{id_borrador}'),
        ]])

    @staticmethod
    def teclado_aviso_tw(id_borrador):
        """El botón del primer aviso de tweet nuevo. A diferencia de teclado_m4, recibe el id
        REAL del borrador (el que ya existe en la tabla borradores) -- ver
        DetectorTweetsNuevos.avisar_tweet_nuevo() para por qué el orden importa acá: este teclado
        se arma después de crear el borrador, nunca antes. Este es justamente el arreglo del bug
        real de producción: antes n8n armaba callback_data=tw:<id de TWEET> antes incluso de crear
        el borrador."""
        return InlineKeyboardMarkup([[
            InlineKeyboardButton('🐦 Twittear algo parecido', callback_data=f'tw:{id_borrador}'),
        ]])

    @staticmethod
    def tiene_boton_tw(mensaje):
        """True si `mensaje` todavía muestra un botón con callback_data tw:<id>."""
        if mensaje is None or mensaje.reply_markup is None:
            return False
        return any(
            isinstance(boton.callback_data, str) and boton.callback_data.startswith('tw:')
            for fila in mensaje.reply_markup.inline_keyboard
            for boton in fila
        )

    @staticmethod
    def extraer_id_tw(mensaje):
        """Busca un botón callback_data=tw:<id> en `mensaje` y devuelve <id>, o None si no hay."""
        if mensaje is None or mensaje.reply_markup is None:
            return None
        for fila in mensaje.reply_markup.inline_keyboard:
            for boton in fila:
                if isinstance(boton.callback_data, str) and boton.callback_data.startswith('tw:'):
                    return boton.callback_data[len('tw:'):]
        return None

    @staticmethod
    async def sacar_boton(bot, chat_id, message_id):
        """Saca el teclado del aviso `message_id`. Devuelve True si esta llamada lo sacó
        (primer toque/reply) y False si ya estaba sacado (toque o reply repetido que venía en
        vuelo).

        Se apoya en el propio Telegram como árbitro en vez de guardar un estado propio: si el
        teclado ya no está, editMessageReplyMarkup devuelve "message is not modified" en lugar de
        aplicar el cambio. Así no hace falta memoria compartida ni una fila en estado.db para
        decidir quién llegó primero, y funciona igual si el proceso se reinició entre un toque y
        el siguiente.

        Genérica (no depende de qué botón sea): la usan tanto la carrera de tw: como la de M4
        (pub:/edit:/desc:), pasándole cada vez el chat_id/message_id de la tarjeta que
        corresponda. Por eso queda como staticmethod en vez de método de instancia: no necesita
        ningún estado de ConversacionTelegram.
        """
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            return True
        except BadRequest as e:
            if 'message is not modified' in str(e).lower():
                return False
            raise

    async def validar_confirmando(self, query, context, id_borrador):
        """Pasos 1-2 comunes a pub:/edit:/desc:: borrador existente, del chat correcto, en paso
        'confirmando', y esta llamada gana la carrera sacándole el teclado a la tarjeta (M4).

        Devuelve la fila si se puede seguir. Si no, ya hizo query.answer() (con TEXTO_YA_PROCESADO
        o el error genérico) y el llamador debe cortar sin tocar nada más.
        """
        mensaje = query.message
        fila = self.estado_db.obtener_borrador(id_borrador)

        if fila is None or fila['chat_id'] != mensaje.chat_id or fila['paso'] != 'confirmando':
            await query.answer(TEXTO_YA_PROCESADO)
            logger.info(
                "callback data=%s borrador_id=%s resultado=ignorado_vencido_o_invalido",
                query.data, id_borrador,
            )
            return None

        try:
            gano = await self.sacar_boton(context.bot, fila['chat_id'], fila['card_message_id'])
        except TelegramError:
            logger.exception("No se pudo sacar el teclado de M4 del borrador_id=%s", id_borrador)
            await query.answer(TEXTO_ERROR_GENERICO)
            return None

        if not gano:
            await query.answer(TEXTO_YA_PROCESADO)
            logger.info(
                "callback data=%s borrador_id=%s resultado=ignorado_boton_ya_sacado",
                query.data, id_borrador,
            )
            return None

        return fila

    async def avanzar_a_esperando_texto(self, context, chat_id, card_message_id, id_borrador, tweet_texto, opciones):
        """Gana la carrera de un botón (tw:, o edit: desde M4) y ya tiene las propuestas de IA
        (recién generadas, o reofrecidas desde `opciones`) -- manda el mensaje pidiendo el texto
        final como ForceReply y recién si eso sale bien persiste
        card_message_id/opciones/paso/prompt_message_id.

        `opciones` puede venir vacía ([]) -- procesar_tw cae acá con propuestas vacías cuando
        Gemini falló (cualquier motivo: cuota agotada, timeout, red), como fallback para no
        trabar al usuario sin forma de tuitear: en vez de las 3 propuestas numeradas, el mensaje
        solo pide el texto directamente (ver texto_pedir_texto_manual). procesar_texto_respuesta
        ya maneja bien una respuesta '1'/'2'/'3' contra una lista vacía (cae a texto propio).

        El orden importa -- ver el caso borde documentado en el pedido de Gige: si el ForceReply
        falla, el teclado ya no se puede recuperar, así que el borrador queda como estaba (sin
        reintento automático) en vez de avanzar a un paso sin forma de pedir el texto.
        """
        fila = self.estado_db.obtener_borrador(id_borrador)

        if fila is None:
            logger.error(
                "borrador_id=%s no existe en borradores, no se puede avanzar a esperando_texto",
                id_borrador,
            )
            await context.bot.send_message(chat_id=chat_id, text=TEXTO_ERROR_GENERICO)
            return False

        paso_anterior = fila['paso']
        texto_prompt = texto_opciones(tweet_texto, opciones) if opciones else texto_pedir_texto_manual(tweet_texto)

        try:
            prompt = await context.bot.send_message(
                chat_id=chat_id,
                text=texto_prompt,
                reply_markup=ForceReply(selective=True),
            )
        except TelegramError:
            logger.exception(
                "No se pudo mandar el mensaje de opciones para borrador_id=%s (paso queda en %s)",
                id_borrador, paso_anterior,
            )
            await context.bot.send_message(chat_id=chat_id, text=TEXTO_ERROR_GENERICO)
            return False

        self.estado_db.marcar_esperando_texto(
            id_borrador, chat_id, card_message_id, prompt.message_id,
            json.dumps(opciones, ensure_ascii=False),
        )

        logger.info(
            "borrador_id=%s paso_anterior=%s paso_nuevo=esperando_texto prompt_message_id=%s resultado=ok",
            id_borrador, paso_anterior, prompt.message_id,
        )
        return True

    async def procesar_tw(self, context, chat_id, aviso_message_id, id_borrador):
        """Común a manejar_tw (botón tw: tocado) y al fallback de on_reply (reply directo al
        aviso sin tocar el botón): en ambos casos ya se ganó la carrera sacándole el teclado al
        aviso, y acá falta generar las 3 propuestas con la IA y mandarlas. Separado para no
        duplicar esta lógica entre los dos puntos de entrada.
        """
        fila = self.estado_db.obtener_borrador(id_borrador)

        if fila is None:
            logger.error(
                "borrador_id=%s no existe en borradores, no se pueden generar propuestas", id_borrador,
            )
            await context.bot.send_message(chat_id=chat_id, text=TEXTO_ERROR_GENERICO)
            return

        try:
            opciones = await asyncio.wait_for(
                self.cliente_gemini.generar_propuestas(fila['tweet_texto']),
                timeout=ClienteGemini.TIMEOUT_TOTAL_IA_SEGUNDOS,
            )
        except Exception:
            # Amplio a propósito: una falla de red, de rate limit (cuota agotada), de parseo del
            # JSON, de formato inesperado, o un asyncio.TimeoutError (ver
            # ClienteGemini.TIMEOUT_SDK_MS/TIMEOUT_TOTAL_IA_SEGUNDOS -- esto es lo que evita que un
            # cuelgue de Gemini congele el bot entero, dado que corre con
            # max_concurrent_updates=1) se manejan todas igual acá: en vez de trabar al usuario
            # con un error sin forma de seguir, se cae a "escribir el texto a mano" (opciones
            # vacías -- ver avanzar_a_esperando_texto/texto_pedir_texto_manual). El propio mensaje
            # de ese fallback ya explica el motivo, así que no hace falta un aviso aparte acá.
            logger.exception(
                "Falló la generación de propuestas para borrador_id=%s -- cae a texto manual",
                id_borrador,
            )
            opciones = []

        await self.avanzar_a_esperando_texto(
            context, chat_id, aviso_message_id, id_borrador, fila['tweet_texto'], opciones,
        )

    async def manejar_tw(self, query, context, data):
        mensaje = query.message
        id_borrador = data.split(':', 1)[1]

        try:
            gano = await self.sacar_boton(context.bot, mensaje.chat_id, mensaje.message_id)
        except TelegramError:
            logger.exception("No se pudo sacar el teclado del aviso %s", mensaje.message_id)
            await query.answer(TEXTO_ERROR_GENERICO)
            return

        if not gano:
            await query.answer(TEXTO_BOTON_VENCIDO)
            logger.info(
                "callback message_id=%s callback_data=%s resultado=ignorado_boton_ya_sacado",
                mensaje.message_id, data,
            )
            return

        await query.answer("Generando propuestas con IA...")
        await self.procesar_tw(context, mensaje.chat_id, mensaje.message_id, id_borrador)

    async def pedir_publicar(self, texto):
        """POST /publicar contra bot-tweets. Clasifica los propios errores de conexión de ESTA
        llamada (bot_telegram -> bot_x) con el mismo criterio de tres estados que
        ClienteX.publicar_tweet usa del otro lado (bot_x -> X): una conexión rechazada significa
        que bot-tweets nunca llegó a intentar nada (error limpio, retryable); un timeout o corte
        después de conectar significa que bot-tweets pudo haber llegado a mandarlo a X sin que
        nos enteremos (ambiguo, no retryable solo)."""
        try:
            async with httpx.AsyncClient() as client:
                respuesta = await client.post(
                    f'{self.BOT_TWEETS_URL}/publicar', json={'texto': texto},
                    timeout=self.TIMEOUT_PUBLICAR_SEGUNDOS,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return {'resultado': 'error', 'detalle': f'no se pudo conectar a bot-tweets: {e}'}
        except httpx.HTTPError as e:
            return {'resultado': 'ambiguo', 'detalle': f'sin respuesta de bot-tweets: {e}'}

        try:
            return respuesta.json()
        except Exception:
            return {
                'resultado': 'ambiguo',
                'detalle': f'respuesta no-JSON de bot-tweets (status {respuesta.status_code})',
            }

    async def manejar_pub(self, query, context, data):
        partes = data.split(':', 2)
        if len(partes) != 3:
            await query.answer(TEXTO_YA_PROCESADO)
            return
        _, id_borrador, hash6_callback = partes

        fila = await self.validar_confirmando(query, context, id_borrador)
        if fila is None:
            return

        texto_final = fila['texto_final']
        hash6_actual = self.calcular_hash6(texto_final)

        if hash6_actual != hash6_callback:
            await query.answer(TEXTO_TEXTO_CAMBIO)
            await context.bot.edit_message_text(
                chat_id=fila['chat_id'], message_id=fila['card_message_id'],
                text=texto_confirmacion(texto_final), reply_markup=self.teclado_m4(id_borrador, texto_final),
            )
            logger.info(
                "borrador_id=%s paso=confirmando resultado=hash_desactualizado", id_borrador,
            )
            return

        if MODO_PUBLICACION == 'mock':
            await self._manejar_pub_mock(query, context, fila, id_borrador, texto_final)
            return
        await self._manejar_pub_real(query, context, fila, id_borrador, texto_final)

    async def _manejar_pub_mock(self, query, context, fila, id_borrador, texto_final):
        gano = self.estado_db.registrar_publicacion(id_borrador)
        if not gano:
            # El INSERT en publicaciones (el cerrojo real) ya lo tomó otro toque -- este solo
            # avisa al usuario que ya está resuelto, sin volver a "publicar" nada.
            await query.answer(TEXTO_YA_PROCESADO)
            await context.bot.edit_message_text(
                chat_id=fila['chat_id'], message_id=fila['card_message_id'],
                text=texto_publicado(texto_final),
            )
            logger.info(
                "borrador_id=%s paso_anterior=confirmando paso_nuevo=publicado_simulado "
                "resultado=idempotente_ya_publicado",
                id_borrador,
            )
            return

        logger.info(
            "borrador_id=%s paso_anterior=confirmando paso_nuevo=publicado_simulado resultado=ok",
            id_borrador,
        )

        await query.answer()
        try:
            # Simulado: no hay publicación real a X, así que no hay espera -- se muestra
            # "publicando" y al toque el resultado final. Ambas ediciones son solo la vidriera del
            # estado que ya quedó escrito en registrar_publicacion.
            await context.bot.edit_message_text(
                chat_id=fila['chat_id'], message_id=fila['card_message_id'], text=TEXTO_PUBLICANDO,
            )
            await context.bot.edit_message_text(
                chat_id=fila['chat_id'], message_id=fila['card_message_id'], text=texto_publicado(texto_final),
            )
        except TelegramError:
            logger.exception(
                "No se pudo actualizar M4 del borrador_id=%s (el tweet simulado ya se procesó; la "
                "tarjeta quedó desactualizada)", id_borrador,
            )
            await context.bot.send_message(
                chat_id=fila['chat_id'],
                text=f"{TEXTO_ERROR_GENERICO} El tweet (simulado) sí se procesó, aunque no se pudo "
                     "mostrar el resultado en la tarjeta.",
            )

    async def _manejar_pub_real(self, query, context, fila, id_borrador, texto_final):
        gano = self.estado_db.tomar_candado_publicacion(id_borrador)
        if not gano:
            # El candado ya está tomado -- en 'publicando' (una publicación real en curso, o que
            # murió a mitad de camino y por eso mismo no se reclama sola), 'real' (ya se publicó)
            # o 'ambiguo' (esperando revisión manual). En los tres casos: no se reintenta nada,
            # solo se avisa que ya hay algo en curso o resuelto.
            await query.answer(TEXTO_YA_PROCESADO)
            logger.info(
                "borrador_id=%s paso_anterior=confirmando resultado=candado_publicacion_ya_tomado",
                id_borrador,
            )
            return

        # A partir de acá el candado ya está tomado (borradores.paso='publicando' en DB). Todo el
        # tramo hasta resolver_publicacion() va envuelto en un except Exception amplio: si algo no
        # previsto revienta acá (un TelegramError en el query.answer() de abajo -- a diferencia del
        # edit_message_text, no tenía try propio --, o cualquier otra excepción imprevista antes de
        # llegar a clasificar el resultado), sin esto el candado quedaba trabado en 'publicando'
        # para siempre: un estado atascado silencioso, sin pasar por 'ambiguo' ni avisar al usuario
        # con TEXTO_REVISION_MANUAL -- peor que el caso 'ambiguo' ya contemplado, porque no queda
        # ningún rastro claro en el chat. Ante cualquier excepción de esta clase, se fuerza el
        # candado a 'ambiguo' (mismo criterio conservador que ya usa el resto del método ante
        # cualquier duda) en vez de dejarlo colgado.
        resuelto = False
        try:
            await query.answer()
            try:
                await context.bot.edit_message_text(
                    chat_id=fila['chat_id'], message_id=fila['card_message_id'], text=TEXTO_PUBLICANDO_REAL,
                )
            except TelegramError:
                logger.exception(
                    "No se pudo editar a 'publicando' el borrador_id=%s (se sigue publicando igual)",
                    id_borrador,
                )

            resultado = await self.pedir_publicar(texto_final)
            estado = resultado.get('resultado')

            if estado == 'real':
                self.estado_db.resolver_publicacion(id_borrador, 'real', x_tweet_id=resultado.get('id'))
                resuelto = True
                logger.info(
                    "borrador_id=%s paso_anterior=confirmando paso_nuevo=publicado x_tweet_id=%s "
                    "resultado=ok",
                    id_borrador, resultado.get('id'),
                )
                texto_msg = texto_publicado_real(texto_final, resultado.get('url', ''))
            elif estado == 'error':
                self.estado_db.resolver_publicacion(id_borrador, 'error')
                resuelto = True
                logger.error(
                    "borrador_id=%s paso_anterior=confirmando paso_nuevo=confirmando "
                    "resultado=error_publicacion detalle=%s",
                    id_borrador, resultado.get('detalle'),
                )
                texto_msg = None  # se re-arma más abajo con el teclado M4, es un caso retryable.
            else:
                # 'ambiguo', o cualquier valor inesperado en 'resultado' (defensivo: si /publicar
                # alguna vez devuelve algo que no matchea ninguno de los tres, tratarlo como ambiguo
                # es lo seguro -- nunca asumir que no se publicó).
                self.estado_db.resolver_publicacion(id_borrador, 'ambiguo')
                resuelto = True
                logger.error(
                    "borrador_id=%s paso_anterior=confirmando paso_nuevo=revision_manual "
                    "resultado=ambiguo detalle=%s",
                    id_borrador, resultado.get('detalle'),
                )
                texto_msg = TEXTO_REVISION_MANUAL
        except Exception:
            logger.exception(
                "borrador_id=%s: excepción inesperada entre tomar el candado de publicación y "
                "resolverlo (resuelto=%s) -- %s",
                id_borrador, resuelto,
                "el candado ya había quedado resuelto antes de la excepción, no se vuelve a tocar"
                if resuelto else
                "se fuerza el candado a 'ambiguo' para no dejarlo trabado en 'publicando' sin aviso",
            )
            if resuelto:
                # resolver_publicacion ya corrió bien (el candado quedó en real/error/ambiguo según
                # corresponda) -- lo que falló fue algo posterior (armar el mensaje, loguear). No
                # hay que volver a tocar el candado: forzarlo a 'ambiguo' acá pisaría un 'real' ya
                # confirmado (y le borraría el x_tweet_id guardado) o un 'error' ya liberado.
                return
            try:
                self.estado_db.resolver_publicacion(id_borrador, 'ambiguo')
            except Exception:
                logger.critical(
                    "borrador_id=%s: no se pudo ni siquiera forzar el candado a 'ambiguo' tras la "
                    "excepción de arriba -- revisar a mano la tabla publicaciones/borradores en "
                    "estado.db para este borrador_id",
                    id_borrador,
                )
                return
            estado = 'ambiguo'
            texto_msg = TEXTO_REVISION_MANUAL

        try:
            if estado == 'error':
                await context.bot.edit_message_text(
                    chat_id=fila['chat_id'], message_id=fila['card_message_id'],
                    text=texto_confirmacion(texto_final) + '\n\n' + texto_error_publicacion(resultado.get('detalle', '')),
                    reply_markup=self.teclado_m4(id_borrador, texto_final),
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=fila['chat_id'], message_id=fila['card_message_id'], text=texto_msg,
                )
        except TelegramError:
            logger.exception(
                "No se pudo actualizar M4 del borrador_id=%s tras resolver_publicacion(%s) -- el "
                "estado real ya quedó guardado en DB, solo falló mostrarlo en la tarjeta",
                id_borrador, estado,
            )
            await context.bot.send_message(chat_id=fila['chat_id'], text=TEXTO_ERROR_GENERICO)

    async def manejar_edit(self, query, context, data):
        id_borrador = data.split(':', 1)[1]

        fila = await self.validar_confirmando(query, context, id_borrador)
        if fila is None:
            return

        await query.answer()
        # Reofrece las mismas 3 propuestas ya generadas -- no hace falta gastar otra llamada a la
        # IA solo porque el usuario quiere volver a elegir (o escribir texto propio) sobre el
        # mismo tweet. Si en el futuro se quiere variar las propuestas en cada vuelta de "seguir
        # editando", alcanza con llamar a self.cliente_gemini.generar_propuestas() acá en vez de
        # reusar fila['opciones'].
        opciones = json.loads(fila['opciones'] or '[]')
        ok = await self.avanzar_a_esperando_texto(
            context, fila['chat_id'], fila['card_message_id'], id_borrador, fila['tweet_texto'], opciones,
        )
        if not ok:
            return  # ya logueado y avisado adentro de avanzar_a_esperando_texto.

        await context.bot.edit_message_text(
            chat_id=fila['chat_id'], message_id=fila['card_message_id'], text=texto_esperando_eleccion(opciones),
        )

    async def manejar_desc(self, query, context, data):
        id_borrador = data.split(':', 1)[1]

        fila = await self.validar_confirmando(query, context, id_borrador)
        if fila is None:
            return

        self.estado_db.marcar_cancelado(id_borrador)

        await query.answer()
        await context.bot.edit_message_text(
            chat_id=fila['chat_id'], message_id=fila['card_message_id'], text=TEXTO_DESCARTADO,
        )
        logger.info(
            "borrador_id=%s paso_anterior=confirmando paso_nuevo=cancelado resultado=ok", id_borrador,
        )

    async def procesar_texto_respuesta(self, mensaje, context, fila):
        """Valida el texto que llegó como reply al mensaje de opciones de `fila` (borrador en
        esperando_texto): si es exactamente '1', '2' o '3' toma esa propuesta ya generada; si es
        cualquier otro texto, lo trata como texto propio con las mismas validaciones de siempre
        (vacío, largo)."""
        id_borrador = fila['id']
        texto_bruto = (mensaje.text or '').strip()

        elegida = None
        if texto_bruto in ('1', '2', '3'):
            opciones = json.loads(fila['opciones'] or '[]')
            indice = int(texto_bruto) - 1
            if indice < len(opciones):
                texto_final = opciones[indice]
                elegida = int(texto_bruto)
            else:
                # No debería pasar (siempre generamos/guardamos 3), pero si opciones quedó corta
                # por algún motivo, tratamos el '1'/'2'/'3' como si fuera texto propio en vez de
                # reventar.
                texto_final = texto_bruto
        else:
            texto_final = texto_bruto

        if elegida is None:
            if texto_final == '':
                await self.pedir_texto_de_nuevo(context, fila, TEXTO_PEDIR_TEXTO_VACIO)
                logger.info(
                    "borrador_id=%s paso=esperando_texto resultado=texto_vacio", id_borrador,
                )
                return

            if len(texto_final) > MAX_CARACTERES:
                await self.pedir_texto_de_nuevo(context, fila, texto_pedir_texto_largo(len(texto_final)))
                logger.info(
                    "borrador_id=%s paso=esperando_texto resultado=texto_largo largo=%s",
                    id_borrador, len(texto_final),
                )
                return

        self.estado_db.marcar_confirmando(id_borrador, texto_final, elegida)

        await context.bot.edit_message_text(
            chat_id=fila['chat_id'], message_id=fila['card_message_id'],
            text=texto_confirmacion(texto_final), reply_markup=self.teclado_m4(id_borrador, texto_final),
        )
        logger.info(
            "borrador_id=%s paso_anterior=esperando_texto paso_nuevo=confirmando elegida=%s "
            "resultado=ok",
            id_borrador, elegida,
        )

    async def pedir_texto_de_nuevo(self, context, fila, texto_aviso):
        """Manda un ForceReply nuevo (texto vacío o demasiado largo) y pisa prompt_message_id.
        No toca card_message_id ni su contenido -- el paso se queda en esperando_texto."""
        prompt = await context.bot.send_message(
            chat_id=fila['chat_id'], text=texto_aviso, reply_markup=ForceReply(selective=True),
        )
        self.estado_db.actualizar_prompt_message_id(fila['id'], prompt.message_id)

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        data = query.data or ''
        mensaje = query.message

        if mensaje is None or mensaje.chat_id != self.chat_id:
            return  # otro chat, o callback sin mensaje: se ignora sin responder.

        if data.startswith('tw:'):
            await self.manejar_tw(query, context, data)
        elif data.startswith('pub:'):
            await self.manejar_pub(query, context, data)
        elif data.startswith('edit:'):
            await self.manejar_edit(query, context, data)
        elif data.startswith('desc:'):
            await self.manejar_desc(query, context, data)
        # cualquier otro callback_data no es nuestro: se ignora sin responder.

    async def on_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        mensaje = update.effective_message
        original = mensaje.reply_to_message

        if mensaje.chat_id != self.chat_id or original is None:
            return  # mensaje suelto o de otro chat: ni siquiera es candidato a nuestro flujo.

        fila = self.estado_db.obtener_borrador_esperando_texto(mensaje.chat_id, original.message_id)

        if fila is not None:
            await self.procesar_texto_respuesta(mensaje, context, fila)
            return

        # No es reply a un ForceReply nuestro pendiente: no asumimos que es un reply al aviso tw:,
        # seguimos cayendo a la lógica existente (que sí filtra por tiene_boton_tw).
        if original.from_user is None or not original.from_user.is_bot:
            return  # reply a un mensaje que no es del bot: no es candidato a ningún flujo nuestro.

        if not self.tiene_boton_tw(original):
            # El objeto `original` es una foto del mensaje al momento en que se mandó, no se
            # actualiza si después se le sacó el teclado (por eso no sirve como árbitro de toque
            # repetido, solo para decidir si esto es un reply a un aviso de tweet). Si nunca tuvo
            # botón, no es un reply a un aviso nuestro.
            logger.info(
                "reply message_id=%s original_message_id=%s resultado=ignorado_sin_boton",
                mensaje.message_id, original.message_id,
            )
            return

        try:
            gano = await self.sacar_boton(context.bot, original.chat_id, original.message_id)
        except TelegramError:
            logger.exception("No se pudo sacar el teclado del aviso %s", original.message_id)
            return

        if not gano:
            # Un toque de botón (o el reply de otro mensaje al mismo aviso) ya sacó el teclado.
            logger.info(
                "reply message_id=%s original_message_id=%s resultado=ignorado_boton_ya_sacado",
                mensaje.message_id, original.message_id,
            )
            return

        id_borrador = self.extraer_id_tw(original)
        if id_borrador is None:
            logger.error(
                "reply message_id=%s original_message_id=%s: se sacó el teclado pero no se pudo "
                "extraer el id de tw: del callback_data",
                mensaje.message_id, original.message_id,
            )
            await context.bot.send_message(chat_id=mensaje.chat_id, text=TEXTO_ERROR_GENERICO)
            return

        # Mismo camino que manejar_tw a partir de acá (generar propuestas y ofrecerlas) -- ver
        # procesar_tw. A diferencia del botón, acá no hay query.answer() para dar feedback
        # inmediato de que se está generando, así que se manda un aviso explícito (la llamada a
        # Gemini puede tardar hasta ClienteGemini.TIMEOUT_TOTAL_IA_SEGUNDOS).
        await context.bot.send_message(chat_id=original.chat_id, text="Generando propuestas con IA...")
        await self.procesar_tw(context, original.chat_id, original.message_id, id_borrador)

    async def on_error(self, update, context: ContextTypes.DEFAULT_TYPE):
        logger.error("Excepción sin manejar procesando %r", update, exc_info=context.error)


# --- Instancias de proceso y arranque ---

estado_db = EstadoDB()
cliente_gemini = ClienteGemini()
detector_tweets = DetectorTweetsNuevos(estado_db, CHAT_ID)
conversacion = ConversacionTelegram(estado_db, cliente_gemini, CHAT_ID)


def main():
    token = os.environ['TELEGRAM_BOT_TOKEN']
    # Idéntico a bot_x.py: ambos procesos llaman a crear_tablas_borradores() por si este arranca
    # antes de que bot-tweets haya creado las tablas (docker-compose no fuerza un orden entre los
    # servicios). crear_tabla_ultimos_tweets() solo la necesita este proceso -- ver EstadoDB.
    estado_db.crear_tablas_borradores()
    estado_db.crear_tabla_ultimos_tweets()

    app = Application.builder().token(token).build()

    # Arreglo de Palito (Crítico, confirmado contra la librería real): python-telegram-bot==22.8
    # sin el extra [job-queue] deja Application.job_queue en None sin romper nada -- solo un
    # warning fácil de no notar. Sin esto el chequeo periódico de tweets nuevos simplemente
    # nunca se ejecutaría, en silencio. Falla ruidoso acá para que, si esto se vuelve a romper en
    # el futuro (alguien edita requirements.txt sin saber esto), se note al arrancar y no meses
    # después notando que no llegan avisos.
    if app.job_queue is None:
        raise RuntimeError(
            "app.job_queue es None: falta el extra [job-queue] de python-telegram-bot en "
            "requirements.txt (tiene que decir python-telegram-bot[job-queue]==22.8, no solo "
            "python-telegram-bot==22.8). Sin esto el chequeo periódico de tweets nuevos no "
            "corre nunca."
        )
    app.job_queue.run_repeating(
        detector_tweets.chequeo_periodico, interval=DetectorTweetsNuevos.INTERVALO_CHEQUEO_SEGUNDOS, first=10,
    )

    app.add_handler(CallbackQueryHandler(conversacion.on_callback))
    app.add_handler(MessageHandler(filters.REPLY, conversacion.on_reply))
    app.add_error_handler(conversacion.on_error)
    logger.info("Poller de Telegram arrancando (long polling)...")
    app.run_polling()


if __name__ == '__main__':
    main()
