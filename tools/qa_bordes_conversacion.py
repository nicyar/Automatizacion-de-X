"""QA de casos borde de ConversacionTelegram que NO dependen de Gemini (bypasea la generacion de
propuestas insertando el borrador directo en paso 'confirmando', igual que
tools/qa_publicacion_real.py) -- necesario porque la cuota diaria gratuita de Gemini se agoto
durante esta pasada de QA (ver hallazgo aparte).

Cubre: doble toque de Publicar en modo MOCK (candado de registrar_publicacion), tocar botones
sobre una tarjeta ya resuelta/vencida, y un reply a un ForceReply que ya no esta pendiente.

Requiere MODO_PUBLICACION=mock (aborta si no) -- incidente real conocido si esto no se respeta,
ver tools/qa_conversacion.py.

Uso:
    docker cp tools/qa_bordes_conversacion.py x-telegram-poller-1:/tmp/qa_bordes_conversacion.py
    docker exec x-telegram-poller-1 python /tmp/qa_bordes_conversacion.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')
import bot_telegram as bt  # noqa: E402
from telegram import Bot  # noqa: E402

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = bt.CHAT_ID


class FakeMessage:
    def __init__(self, chat_id, message_id, text=None, reply_to_message=None):
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.reply_to_message = reply_to_message
        self.reply_markup = None
        self.from_user = None


class FakeQuery:
    def __init__(self, data, chat_id, message_id):
        self.data = data
        self.message = FakeMessage(chat_id, message_id)
        self.answered_con = None

    async def answer(self, text=None, **kwargs):
        self.answered_con = text


class FakeUpdate:
    def __init__(self, callback_query=None, effective_message=None):
        self.callback_query = callback_query
        self.effective_message = effective_message


class FakeContext:
    def __init__(self, bot):
        self.bot = bot


def ahora():
    return datetime.now(timezone.utc).isoformat()


def crear_confirmando(id_borrador, texto_final, card_message_id):
    conn = bt.estado_db._conectar()
    try:
        conn.execute(
            '''INSERT INTO borradores
               (id, chat_id, card_message_id, tweet_id, cuenta, tweet_texto, tweet_fecha,
                texto_final, elegida, paso, creado_en, actualizado_en)
               VALUES (?, ?, ?, ?, 'QA', 'tweet de prueba QA', ?, ?, 1, 'confirmando', ?, ?)''',
            (id_borrador, CHAT_ID, card_message_id, f'qaborde-{id_borrador}', ahora(),
             texto_final, ahora(), ahora()),
        )
        conn.commit()
    finally:
        conn.close()


async def enviar_tarjeta_m4(bot, texto_final):
    id_borrador = bt.estado_db.generar_id_borrador()
    m = await bot.send_message(
        chat_id=CHAT_ID,
        text=bt.texto_confirmacion(texto_final),
        reply_markup=bt.conversacion.teclado_m4(id_borrador, texto_final),
    )
    crear_confirmando(id_borrador, texto_final, m.message_id)
    return id_borrador, m


def fila(id_borrador):
    conn = bt.estado_db._conectar()
    try:
        return conn.execute('SELECT * FROM borradores WHERE id = ?', (id_borrador,)).fetchone()
    finally:
        conn.close()


def contar_publicaciones(id_borrador):
    conn = bt.estado_db._conectar()
    try:
        return conn.execute(
            'SELECT COUNT(*) AS c FROM publicaciones WHERE borrador_id = ?', (id_borrador,)
        ).fetchone()['c']
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


async def caso_doble_toque_mock(bot, context):
    print("=== Caso 1: doble toque SIMULTANEO de Publicar en modo MOCK ===")
    texto = "[QA bordes] doble toque mock"
    id_b, m = await enviar_tarjeta_m4(bot, texto)
    hash6 = bt.conversacion.calcular_hash6(texto)
    data_pub = f'pub:{id_b}:{hash6}'

    q_a = FakeQuery(data_pub, CHAT_ID, m.message_id)
    q_b = FakeQuery(data_pub, CHAT_ID, m.message_id)
    await asyncio.gather(
        bt.conversacion.on_callback(FakeUpdate(callback_query=q_a), context),
        bt.conversacion.on_callback(FakeUpdate(callback_query=q_b), context),
    )
    f = fila(id_b)
    n_pub = contar_publicaciones(id_b)
    check(f['paso'] == 'publicado_simulado', f"paso == publicado_simulado (es {f['paso']!r})")
    check(n_pub == 1, f"exactamente 1 fila en publicaciones (hay {n_pub})")
    print(f"  respuestas: q_a={q_a.answered_con!r} q_b={q_b.answered_con!r}")

    await bot.delete_message(chat_id=CHAT_ID, message_id=m.message_id)
    limpiar(id_b)
    print("  Caso 1: OK\n")


async def caso_boton_sobre_tarjeta_vencida(bot, context):
    print("=== Caso 2: tocar Publicar/Editar/Descartar sobre una tarjeta YA resuelta ===")
    texto = "[QA bordes] tarjeta ya resuelta"
    id_b, m = await enviar_tarjeta_m4(bot, texto)
    hash6 = bt.conversacion.calcular_hash6(texto)

    # La resolvemos primero (Descartar), simulando que ya se proceso.
    q0 = FakeQuery(f'desc:{id_b}', CHAT_ID, m.message_id)
    await bt.conversacion.on_callback(FakeUpdate(callback_query=q0), context)
    f0 = fila(id_b)
    check(f0['paso'] == 'cancelado', f"(setup) paso == cancelado (es {f0['paso']!r})")

    # Ahora, sobre la MISMA tarjeta ya resuelta, probamos los tres botones -- ninguno debe hacer nada.
    for prefijo, nombre in (('pub', 'Publicar'), ('edit', 'Editar'), ('desc', 'Descartar')):
        data = f'{prefijo}:{id_b}:{hash6}' if prefijo == 'pub' else f'{prefijo}:{id_b}'
        q = FakeQuery(data, CHAT_ID, m.message_id)
        await bt.conversacion.on_callback(FakeUpdate(callback_query=q), context)
        f = fila(id_b)
        check(f['paso'] == 'cancelado', f"{nombre} sobre tarjeta vencida no cambia el paso (quedo en {f['paso']!r})")
        check(q.answered_con == bt.TEXTO_YA_PROCESADO, f"{nombre} sobre tarjeta vencida avisa 'ya procesado' (fue {q.answered_con!r})")

    await bot.delete_message(chat_id=CHAT_ID, message_id=m.message_id)
    limpiar(id_b)
    print("  Caso 2: OK\n")


async def caso_reply_a_forcereply_vencido(bot, context):
    print("=== Caso 3: reply a un ForceReply que YA NO esta pendiente (prompt_message_id viejo) ===")
    texto = "[QA bordes] reply a prompt vencido"
    id_b, m = await enviar_tarjeta_m4(bot, texto)

    # Nunca estuvo en esperando_texto (fue insertado directo en confirmando) -- un reply a
    # cualquier prompt_message_id inventado no debe encontrar match ni romper nada.
    prompt_falso = FakeMessage(CHAT_ID, 9999999)
    reply = FakeMessage(CHAT_ID, 9999998, text="un texto cualquiera", reply_to_message=prompt_falso)
    await bt.conversacion.on_reply(FakeUpdate(effective_message=reply), context)
    f = fila(id_b)
    check(f['paso'] == 'confirmando', f"el borrador no se toco (paso sigue en {f['paso']!r})")

    await bot.delete_message(chat_id=CHAT_ID, message_id=m.message_id)
    limpiar(id_b)
    print("  Caso 3: OK (reply a prompt inexistente se ignora sin romper nada)\n")


async def caso_callback_malformado(bot, context):
    print("=== Caso 4: callback_data malformado / de otro chat / desconocido ===")
    # data sin id_borrador
    q1 = FakeQuery('pub:', CHAT_ID, 123456)
    await bt.conversacion.on_callback(FakeUpdate(callback_query=q1), context)
    print(f"  'pub:' vacio -> respuesta={q1.answered_con!r} (no debe tirar excepcion)")

    # prefijo desconocido
    q2 = FakeQuery('otraaccion:abc123', CHAT_ID, 123456)
    await bt.conversacion.on_callback(FakeUpdate(callback_query=q2), context)
    check(q2.answered_con is None, f"prefijo desconocido se ignora sin responder (fue {q2.answered_con!r})")

    # id_borrador que no existe
    q3 = FakeQuery('pub:noexiste123:abcdef', CHAT_ID, 123456)
    await bt.conversacion.on_callback(FakeUpdate(callback_query=q3), context)
    check(q3.answered_con == bt.TEXTO_YA_PROCESADO, f"id_borrador inexistente -> 'ya procesado' (fue {q3.answered_con!r})")

    print("  Caso 4: OK (nada de esto tiro una excepcion sin manejar)\n")


CASOS = {
    '1': caso_doble_toque_mock,
    '2': caso_boton_sobre_tarjeta_vencida,
    '3': caso_reply_a_forcereply_vencido,
    '4': caso_callback_malformado,
}


async def main():
    print(f"MODO_PUBLICACION efectivo del proceso: {bt.MODO_PUBLICACION!r}")
    if bt.MODO_PUBLICACION != 'mock':
        sys.exit(f"ABORTADO: se requiere MODO_PUBLICACION=mock, es {bt.MODO_PUBLICACION!r}")

    seleccion = sys.argv[1:] or list(CASOS)
    async with Bot(token=TOKEN) as bot:
        context = FakeContext(bot)
        for clave in seleccion:
            await CASOS[clave](bot, context)

    print("TODO OK.")


if __name__ == '__main__':
    asyncio.run(main())
