"""QA adversarial del candado de publicacion REAL (tomar_candado_publicacion / resolver_publicacion
/ _manejar_pub_real), sin gastar tweets reales ni tocar X.

Corre con MODO_PUBLICACION=real (el default de produccion, confirmado: no esta seteado en .env),
usando el bot_telegram.py real tal cual corre en el contenedor -- pero mockeando
ConversacionTelegram.pedir_publicar (el borde HTTP hacia bot-tweets) para no pegarle nunca a X,
salvo en el caso B que llama a la version real para probar bot-tweets REALMENTE caido.

Mismo patron que tools/qa_conversacion.py: mensajes reales contra el chat real via el bot token,
Update/CallbackQuery fabricados a mano, y limpieza al final de cada caso.

Uso (cada caso es independiente, para poder detener/levantar bot-tweets entre medio desde afuera):
    docker cp tools/qa_publicacion_real.py x-telegram-poller-1:/tmp/qa_publicacion_real.py
    docker exec x-telegram-poller-1 python /tmp/qa_publicacion_real.py A
    docker exec x-telegram-poller-1 python /tmp/qa_publicacion_real.py B   # con bot-tweets detenido
    docker exec x-telegram-poller-1 python /tmp/qa_publicacion_real.py C
    docker exec x-telegram-poller-1 python /tmp/qa_publicacion_real.py D
"""
import asyncio
import os
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')
import bot_telegram as bt  # noqa: E402
from telegram import Bot  # noqa: E402

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = bt.CHAT_ID


class FakeMessage:
    def __init__(self, chat_id, message_id):
        self.chat_id = chat_id
        self.message_id = message_id


class FakeQuery:
    def __init__(self, data, chat_id, message_id):
        self.data = data
        self.message = FakeMessage(chat_id, message_id)
        self.answered_con = None

    async def answer(self, text=None, **kwargs):
        self.answered_con = text


class FakeUpdate:
    def __init__(self, callback_query):
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, bot):
        self.bot = bot


def ahora():
    return datetime.now(timezone.utc).isoformat()


def crear_borrador_confirmando(id_borrador, tag, texto_final, card_message_id):
    """Inserta la fila directo en borradores, ya en paso 'confirmando', con el id_borrador que
    YA se uso para armar el teclado M4 del mensaje real -- ver enviar_tarjeta_m4() para por que
    el orden importa: sacar_boton (la carrera del boton M4) necesita que el mensaje real tenga
    de verdad ese teclado puesto, si no ambos toques de Publicar fallan en la carrera del boton
    (editMessageReplyMarkup sobre un mensaje sin teclado da 'message is not modified') antes de
    siquiera llegar al candado de publicacion que este script quiere probar."""
    conn = bt.estado_db._conectar()
    try:
        conn.execute(
            '''INSERT INTO borradores
               (id, chat_id, card_message_id, tweet_id, cuenta, tweet_texto, tweet_fecha,
                texto_final, elegida, paso, creado_en, actualizado_en)
               VALUES (?, ?, ?, ?, 'QA', 'tweet de prueba QA', ?, ?, 1, 'confirmando', ?, ?)''',
            (id_borrador, CHAT_ID, card_message_id, f'qareal-{tag}-{id_borrador}', ahora(),
             texto_final, ahora(), ahora()),
        )
        conn.commit()
    finally:
        conn.close()


async def enviar_tarjeta_m4(bot, texto_final):
    """Manda la tarjeta M4 real (con el teclado Publicar/Seguir editando/Descartar) para un
    id_borrador recien generado, y crea la fila 'confirmando' correspondiente -- asi
    validar_confirmando/sacar_boton ven exactamente lo que verian con un borrador real."""
    id_borrador = bt.estado_db.generar_id_borrador()
    m = await bot.send_message(
        chat_id=CHAT_ID,
        text=bt.texto_confirmacion(texto_final),
        reply_markup=bt.conversacion.teclado_m4(id_borrador, texto_final),
    )
    crear_borrador_confirmando(id_borrador, id_borrador, texto_final, m.message_id)
    return id_borrador, m


def fila(id_borrador):
    conn = bt.estado_db._conectar()
    try:
        return conn.execute('SELECT * FROM borradores WHERE id = ?', (id_borrador,)).fetchone()
    finally:
        conn.close()


def fila_publicacion(id_borrador):
    conn = bt.estado_db._conectar()
    try:
        return conn.execute(
            'SELECT * FROM publicaciones WHERE borrador_id = ?', (id_borrador,)
        ).fetchone()
    finally:
        conn.close()


def limpiar(id_borrador):
    conn = bt.estado_db._conectar()
    try:
        conn.execute('DELETE FROM publicaciones WHERE borrador_id = ?', (id_borrador,))
        conn.execute('DELETE FROM borradores WHERE id = ?', (id_borrador,))
        conn.commit()
    finally:
        conn.close()


def check(cond, msg):
    estado = 'OK' if cond else 'FALLO'
    print(f"  [{estado}] {msg}")
    if not cond:
        raise AssertionError(msg)


async def caso_a(bot, context):
    print("=== Caso A: doble toque SIMULTANEO de Publicar (candado real) ===")
    texto_a = "[QA candado real] Caso A - doble toque simultaneo"
    id_a, m = await enviar_tarjeta_m4(bot, texto_a)
    hash6 = bt.conversacion.calcular_hash6(texto_a)
    data_pub = f'pub:{id_a}:{hash6}'

    llamadas = {'n': 0}
    original = bt.ConversacionTelegram.pedir_publicar

    async def pedir_publicar_lento(self, texto):
        llamadas['n'] += 1
        await asyncio.sleep(0.3)
        return {'resultado': 'real', 'id': '999999999999', 'url': 'https://x.com/fake/status/999999999999'}

    bt.ConversacionTelegram.pedir_publicar = pedir_publicar_lento
    try:
        q_a = FakeQuery(data_pub, CHAT_ID, m.message_id)
        q_b = FakeQuery(data_pub, CHAT_ID, m.message_id)
        await asyncio.gather(
            bt.conversacion.on_callback(FakeUpdate(q_a), context),
            bt.conversacion.on_callback(FakeUpdate(q_b), context),
        )
    finally:
        bt.ConversacionTelegram.pedir_publicar = original

    f = fila(id_a)
    pub = fila_publicacion(id_a)
    check(llamadas['n'] == 1, f"pedir_publicar (HTTP a bot-tweets) se llamo UNA sola vez (se llamo {llamadas['n']} veces)")
    check(f['paso'] == 'publicado', f"paso == publicado (es {f['paso']!r})")
    check(pub is not None and pub['estado'] == 'real', f"publicaciones.estado == real (es {pub['estado'] if pub else None!r})")
    check(pub['x_tweet_id'] == '999999999999', "x_tweet_id guardado correctamente")
    print(f"  respuestas: q_a={q_a.answered_con!r} q_b={q_b.answered_con!r}")
    check(
        (q_a.answered_con == bt.TEXTO_YA_PROCESADO) != (q_b.answered_con == bt.TEXTO_YA_PROCESADO),
        "exactamente uno de los dos toques recibio TEXTO_YA_PROCESADO (el otro gano y publico)",
    )

    await bot.delete_message(chat_id=CHAT_ID, message_id=m.message_id)
    limpiar(id_a)
    print("  Caso A: OK\n")


async def caso_b(bot, context):
    print("=== Caso B: bot-tweets REALMENTE caido (sin mocks, ConnectError real) ===")
    texto_b = "[QA candado real] Caso B - bot-tweets caido"
    id_b, m = await enviar_tarjeta_m4(bot, texto_b)
    hash6_b = bt.conversacion.calcular_hash6(texto_b)

    q_c = FakeQuery(f'pub:{id_b}:{hash6_b}', CHAT_ID, m.message_id)
    await bt.conversacion.on_callback(FakeUpdate(q_c), context)
    f_b = fila(id_b)
    pub_b = fila_publicacion(id_b)
    print(f"  paso resultante: {f_b['paso']!r}, publicaciones.estado: {pub_b['estado'] if pub_b else None!r}")
    check(f_b['paso'] == 'confirmando', f"paso volvio a confirmando -- error retryable, no revision_manual (es {f_b['paso']!r})")
    check(pub_b is not None and pub_b['estado'] == 'error', f"publicaciones.estado == error, NO ambiguo (es {pub_b['estado'] if pub_b else None!r})")

    print("  reintentando Publicar con bot-tweets TODAVIA caido -- el candado 'error' debe ser reclamable")
    q_d = FakeQuery(f'pub:{id_b}:{hash6_b}', CHAT_ID, m.message_id)
    await bt.conversacion.on_callback(FakeUpdate(q_d), context)
    f_b2 = fila(id_b)
    pub_b2 = fila_publicacion(id_b)
    check(f_b2['paso'] == 'confirmando', f"segundo intento tambien retryable (es {f_b2['paso']!r})")
    check(pub_b2['estado'] == 'error', f"sigue en error tras el reintento (es {pub_b2['estado']!r})")

    await bot.delete_message(chat_id=CHAT_ID, message_id=m.message_id)
    limpiar(id_b)
    print("  Caso B: OK\n")


async def caso_c(bot, context):
    print("=== Caso C: resultado 'ambiguo' -> revision_manual, candado NO se libera ===")
    texto_c = "[QA candado real] Caso C - resultado ambiguo"
    id_c, m = await enviar_tarjeta_m4(bot, texto_c)
    hash6_c = bt.conversacion.calcular_hash6(texto_c)

    original = bt.ConversacionTelegram.pedir_publicar

    async def pedir_publicar_ambiguo(self, texto):
        return {'resultado': 'ambiguo', 'detalle': 'QA: simulando timeout de X'}

    bt.ConversacionTelegram.pedir_publicar = pedir_publicar_ambiguo
    try:
        q_e = FakeQuery(f'pub:{id_c}:{hash6_c}', CHAT_ID, m.message_id)
        await bt.conversacion.on_callback(FakeUpdate(q_e), context)
    finally:
        bt.ConversacionTelegram.pedir_publicar = original

    f_c = fila(id_c)
    pub_c = fila_publicacion(id_c)
    check(f_c['paso'] == 'revision_manual', f"paso == revision_manual (es {f_c['paso']!r})")
    check(pub_c is not None and pub_c['estado'] == 'ambiguo', f"publicaciones.estado == ambiguo (es {pub_c['estado'] if pub_c else None!r})")

    print("  reintentando Publicar sobre un borrador en revision_manual -- NO debe reclamar el candado")
    q_f = FakeQuery(f'pub:{id_c}:{hash6_c}', CHAT_ID, m.message_id)
    await bt.conversacion.on_callback(FakeUpdate(q_f), context)
    f_c2 = fila(id_c)
    check(f_c2['paso'] == 'revision_manual', f"sigue en revision_manual, no se reclamo solo (es {f_c2['paso']!r})")
    check(q_f.answered_con == bt.TEXTO_YA_PROCESADO, f"toast de 'ya procesado' al reintentar sobre ambiguo (fue {q_f.answered_con!r})")

    await bot.delete_message(chat_id=CHAT_ID, message_id=m.message_id)
    limpiar(id_c)
    print("  Caso C: OK\n")


async def caso_d(bot, context):
    print("=== Caso D: texto con HTML/emojis/saltos de linea (dentro de 280) ===")
    texto_d = "<script>alert('x')</script> & \"comillas\" \U0001F680\U0001F426\nsegunda linea\ttab"
    id_d, m = await enviar_tarjeta_m4(bot, texto_d)
    hash6_d = bt.conversacion.calcular_hash6(texto_d)

    original = bt.ConversacionTelegram.pedir_publicar
    recibido = {}

    async def pedir_publicar_real_ok(self, texto):
        recibido['texto'] = texto
        return {'resultado': 'real', 'id': '888888888888', 'url': 'https://x.com/fake/status/888888888888'}

    bt.ConversacionTelegram.pedir_publicar = pedir_publicar_real_ok
    try:
        q_g = FakeQuery(f'pub:{id_d}:{hash6_d}', CHAT_ID, m.message_id)
        await bt.conversacion.on_callback(FakeUpdate(q_g), context)
    finally:
        bt.ConversacionTelegram.pedir_publicar = original

    check(recibido.get('texto') == texto_d, "el texto raro llega intacto (sin escapar/mutar) hasta pedir_publicar")
    f_d = fila(id_d)
    check(f_d['paso'] == 'publicado', f"texto raro tambien publica bien en el flujo (paso es {f_d['paso']!r})")

    await bot.delete_message(chat_id=CHAT_ID, message_id=m.message_id)
    limpiar(id_d)
    print("  Caso D: OK (sin excepcion al editar el mensaje con HTML/emojis/tab)\n")


CASOS = {'A': caso_a, 'B': caso_b, 'C': caso_c, 'D': caso_d}


async def main():
    if len(sys.argv) != 2 or sys.argv[1] not in CASOS:
        sys.exit(f"Uso: python {sys.argv[0]} {{{'|'.join(CASOS)}}}")

    print(f"MODO_PUBLICACION efectivo del proceso: {bt.MODO_PUBLICACION!r}")
    check(bt.MODO_PUBLICACION == 'real', "corre en modo real (si no, este script no prueba lo que tiene que probar)")

    async with Bot(token=TOKEN) as bot:
        context = FakeContext(bot)
        await CASOS[sys.argv[1]](bot, context)

    print("TODO OK.")


if __name__ == '__main__':
    asyncio.run(main())
