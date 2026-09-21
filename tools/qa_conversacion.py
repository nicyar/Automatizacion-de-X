"""QA en vivo del flujo completo de conversación de bot_telegram.py (elegir texto, confirmar,
publicar simulado) contra el chat real y estado.db real.

Limitación honesta: este agente no tiene una sesión de usuario de Telegram (solo el token del
bot), así que no puede tocar un botón ni mandar un mensaje *como si fuera* el usuario
dentro de la app real. Lo que sí hace, y es real de punta a punta:
  - Inserta el borrador de prueba directo en estado.db (como pide la consigna).
  - Manda/edita los mensajes de la conversación con el bot token real, contra el chat real
    (CHAT_ID), usando exactamente el código de bot_telegram.py (bt.estado_db para la base y los
    métodos de bt.conversacion -- sacar_boton, avanzar_a_esperando_texto, manejar_pub/edit/desc,
    procesar_texto_respuesta) tal como corren en producción -- se importa el módulo real, no una
    copia.
  - sacar_boton corre contra la API real de Telegram: la carrera del paso 7 (doble Publicar) se
    resuelve con el mismo editMessageReplyMarkup idempotente que se usaría con un toque real, no
    con un mock.
Lo único fabricado es el "disparador" de cada paso (un objeto Update/CallbackQuery armado a mano
en vez de uno que llegó por long polling desde un toque real), porque generar ese evento requiere
la cuenta de Telegram del usuario, no el bot. query.answer() también se reemplaza por un stub que
solo registra el texto (un answerCallbackQuery real fallaría por callback_query_id inválido, dado
que el callback no vino de Telegram).

Corre DENTRO del contenedor del poller (mismo estado.db, mismo bot_telegram.py que producción):
    docker cp tools/qa_conversacion.py x-telegram-poller-1:/tmp/qa_conversacion.py
    docker exec x-telegram-poller-1 python /tmp/qa_conversacion.py
"""
import asyncio
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')
import bot_telegram as bt  # noqa: E402  (después del sys.path.insert)
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


def crear_borrador_prueba(tag):
    conn = bt.estado_db._conectar()
    try:
        id_borrador = secrets.token_hex(4)
        conn.execute(
            '''INSERT INTO borradores
               (id, tweet_id, cuenta, tweet_texto, tweet_fecha, paso, creado_en, actualizado_en)
               VALUES (?, ?, 'QA', 'tweet de prueba QA', ?, 'nuevo', ?, ?)''',
            (id_borrador, f'qa-{tag}-{id_borrador}', ahora(), ahora(), ahora()),
        )
        conn.commit()
        return id_borrador
    finally:
        conn.close()


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


def check(cond, msg):
    estado = 'OK' if cond else 'FALLO'
    print(f"  [{estado}] {msg}")
    if not cond:
        raise AssertionError(msg)


async def main():
    # Guarda de seguridad agregada tras un incidente real (13/09): este script ejercita
    # manejar_pub/registrar_publicacion asumiendo el comportamiento simulado de siempre (Paso 7
    # espera paso=='publicado_simulado'). Con MODO_PUBLICACION=real (el default de producción
    # desde que existe la publicación real), el mismo toque de "Publicar" termina en
    # _manejar_pub_real y publica de verdad en X -- eso fue exactamente lo que pasó la primera
    # vez que se corrió este script contra los contenedores en producción sin este chequeo.
    # Cortar acá en vez de dejar que el resto del script se ejecute a ciegas.
    if bt.MODO_PUBLICACION != 'mock':
        sys.exit(
            f"ABORTADO: MODO_PUBLICACION={bt.MODO_PUBLICACION!r} en este proceso, no 'mock'. "
            "Este script asume publicación simulada (ver Paso 7) -- correrlo con MODO_PUBLICACION=real "
            "publica de verdad en X. Seteá MODO_PUBLICACION=mock en el entorno del contenedor "
            "(.env + recrear telegram-poller) antes de reintentar, o usá "
            "tools/qa_publicacion_real.py si lo que querés probar es justamente el modo real "
            "(ese script sí valida esto y mockea el borde HTTP en vez de publicar de verdad)."
        )

    mensajes_para_borrar = []
    ids_para_borrar = []

    async with Bot(token=TOKEN) as bot:
        context = FakeContext(bot)

        print("=== Paso 1: tocar tw: -> ForceReply, paso=esperando_texto ===")
        id1 = crear_borrador_prueba('borrador1')
        ids_para_borrar.append(id1)
        m1 = await bot.send_message(
            chat_id=CHAT_ID,
            text="[PRUEBA QA] tweet de prueba QA — ignorar",
            reply_markup=bt.InlineKeyboardMarkup(
                [[bt.InlineKeyboardButton('🐦 Twittear algo parecido', callback_data=f'tw:{id1}')]]
            ),
        )
        mensajes_para_borrar.append(m1.message_id)
        query = FakeQuery(f'tw:{id1}', CHAT_ID, m1.message_id)
        await bt.conversacion.on_callback(FakeUpdate(callback_query=query), context)
        f = fila(id1)
        check(f['paso'] == 'esperando_texto', f"paso == esperando_texto (es {f['paso']!r})")
        check(f['card_message_id'] == m1.message_id, "card_message_id == message_id de M1")
        check(f['prompt_message_id'] is not None, "prompt_message_id quedó seteado")
        # Nota: antes de actualizar este script para el refactor a clases, esta aserción
        # comparaba contra bt.TEXTO_PEDIR_TEXTO, una constante que nunca existió en
        # bot_telegram.py (ni antes ni después del refactor) -- quedaba sin ejecutarse nunca
        # porque el resto del script tampoco corría con las referencias rotas de abajo. El toast
        # real que manda manejar_tw al tocar "tw:" es este literal.
        check(query.answered_con == "Generando propuestas con IA...", f"toast de generación (fue {query.answered_con!r})")
        mensajes_para_borrar.append(f['prompt_message_id'])
        print(f"  borrador_id={id1} card_message_id={m1.message_id} prompt_message_id={f['prompt_message_id']}")

        print("=== Paso 2: reply vacío -> pide de nuevo, sigue esperando_texto ===")
        prompt_anterior = f['prompt_message_id']
        original = FakeMessage(CHAT_ID, prompt_anterior)
        reply_vacio = FakeMessage(CHAT_ID, 555000001, text='   ', reply_to_message=original)
        await bt.conversacion.on_reply(FakeUpdate(effective_message=reply_vacio), context)
        f = fila(id1)
        check(f['paso'] == 'esperando_texto', f"paso sigue esperando_texto (es {f['paso']!r})")
        check(f['prompt_message_id'] != prompt_anterior, "prompt_message_id se pisó con uno nuevo")
        mensajes_para_borrar.append(f['prompt_message_id'])
        print(f"  nuevo prompt_message_id={f['prompt_message_id']}")

        print("=== Paso 3: reply de 300 caracteres -> pide de nuevo con aviso de largo ===")
        prompt_anterior = f['prompt_message_id']
        original = FakeMessage(CHAT_ID, prompt_anterior)
        texto_largo = 'a' * 300
        reply_largo = FakeMessage(CHAT_ID, 555000002, text=texto_largo, reply_to_message=original)
        await bt.conversacion.on_reply(FakeUpdate(effective_message=reply_largo), context)
        f = fila(id1)
        check(f['paso'] == 'esperando_texto', f"paso sigue esperando_texto (es {f['paso']!r})")
        check(f['prompt_message_id'] != prompt_anterior, "prompt_message_id se pisó de nuevo")
        mensajes_para_borrar.append(f['prompt_message_id'])
        print(f"  nuevo prompt_message_id={f['prompt_message_id']}")

        print("=== Paso 4: reply con texto válido -> aparece M4, paso=confirmando ===")
        prompt_anterior = f['prompt_message_id']
        original = FakeMessage(CHAT_ID, prompt_anterior)
        texto_valido_1 = "[PRUEBA QA] Este es el primer texto válido de prueba."
        reply_ok = FakeMessage(CHAT_ID, 555000003, text=texto_valido_1, reply_to_message=original)
        await bt.conversacion.on_reply(FakeUpdate(effective_message=reply_ok), context)
        f = fila(id1)
        check(f['paso'] == 'confirmando', f"paso == confirmando (es {f['paso']!r})")
        check(f['texto_final'] == texto_valido_1, "texto_final quedó guardado tal cual")
        print(f"  texto_final={f['texto_final']!r}")

        print('=== Paso 5: tocar "Seguir editando" -> esperando_texto con nuevo ForceReply ===')
        query = FakeQuery(f'edit:{id1}', CHAT_ID, f['card_message_id'])
        await bt.conversacion.on_callback(FakeUpdate(callback_query=query), context)
        f = fila(id1)
        check(f['paso'] == 'esperando_texto', f"paso == esperando_texto (es {f['paso']!r})")
        mensajes_para_borrar.append(f['prompt_message_id'])
        print(f"  nuevo prompt_message_id={f['prompt_message_id']}")

        print("=== Paso 6: reply de nuevo con texto válido -> M4 de nuevo ===")
        prompt_anterior = f['prompt_message_id']
        original = FakeMessage(CHAT_ID, prompt_anterior)
        texto_valido_2 = "[PRUEBA QA] Segundo texto válido, después de Seguir editando."
        reply_ok2 = FakeMessage(CHAT_ID, 555000004, text=texto_valido_2, reply_to_message=original)
        await bt.conversacion.on_reply(FakeUpdate(effective_message=reply_ok2), context)
        f = fila(id1)
        check(f['paso'] == 'confirmando', f"paso == confirmando (es {f['paso']!r})")
        check(f['texto_final'] == texto_valido_2, "texto_final actualizado al segundo texto")
        print(f"  texto_final={f['texto_final']!r}")

        print('=== Paso 7: tocar "Publicar" DOS VECES RÁPIDO -> un solo M6, una sola fila ===')
        hash6 = bt.conversacion.calcular_hash6(f['texto_final'])
        data_pub = f'pub:{id1}:{hash6}'
        q_a = FakeQuery(data_pub, CHAT_ID, f['card_message_id'])
        q_b = FakeQuery(data_pub, CHAT_ID, f['card_message_id'])
        await asyncio.gather(
            bt.conversacion.on_callback(FakeUpdate(callback_query=q_a), context),
            bt.conversacion.on_callback(FakeUpdate(callback_query=q_b), context),
        )
        f = fila(id1)
        n_pub = contar_publicaciones(id1)
        check(f['paso'] == 'publicado_simulado', f"paso == publicado_simulado (es {f['paso']!r})")
        check(n_pub == 1, f"exactamente 1 fila en publicaciones (hay {n_pub})")
        print(f"  respuestas: q_a={q_a.answered_con!r} q_b={q_b.answered_con!r}")
        print(f"  filas en publicaciones para {id1}: {n_pub}")

        print("=== Paso 8: otro borrador de prueba, tocar Descartar -> cancelado ===")
        id2 = crear_borrador_prueba('borrador2')
        ids_para_borrar.append(id2)
        m1b = await bot.send_message(
            chat_id=CHAT_ID,
            text="[PRUEBA QA] segundo tweet de prueba — ignorar",
            reply_markup=bt.InlineKeyboardMarkup(
                [[bt.InlineKeyboardButton('🐦 Twittear algo parecido', callback_data=f'tw:{id2}')]]
            ),
        )
        mensajes_para_borrar.append(m1b.message_id)
        query = FakeQuery(f'tw:{id2}', CHAT_ID, m1b.message_id)
        await bt.conversacion.on_callback(FakeUpdate(callback_query=query), context)
        f2 = fila(id2)
        mensajes_para_borrar.append(f2['prompt_message_id'])
        original2 = FakeMessage(CHAT_ID, f2['prompt_message_id'])
        texto_valido_3 = "[PRUEBA QA] Texto para el borrador que vamos a descartar."
        reply_ok3 = FakeMessage(CHAT_ID, 555000005, text=texto_valido_3, reply_to_message=original2)
        await bt.conversacion.on_reply(FakeUpdate(effective_message=reply_ok3), context)
        f2 = fila(id2)
        check(f2['paso'] == 'confirmando', f"(setup) paso == confirmando antes de descartar (es {f2['paso']!r})")

        query_desc = FakeQuery(f'desc:{id2}', CHAT_ID, f2['card_message_id'])
        await bt.conversacion.on_callback(FakeUpdate(callback_query=query_desc), context)
        f2 = fila(id2)
        check(f2['paso'] == 'cancelado', f"paso == cancelado (es {f2['paso']!r})")
        print(f"  toast descartar={query_desc.answered_con!r}")

        print("\n=== Limpieza: borrando mensajes y filas de prueba ===")
        for mid in mensajes_para_borrar:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=mid)
            except Exception as e:
                print(f"  aviso: no se pudo borrar message_id={mid}: {e}")
        # También borramos las tarjetas M1/M4 finales (quedaron editadas con el resultado).
        for mid in (m1.message_id, m1b.message_id):
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=mid)
            except Exception as e:
                print(f"  aviso: no se pudo borrar message_id={mid}: {e}")

    conn = bt.estado_db._conectar()
    try:
        for id_b in ids_para_borrar:
            conn.execute('DELETE FROM publicaciones WHERE borrador_id = ?', (id_b,))
            conn.execute('DELETE FROM borradores WHERE id = ?', (id_b,))
        conn.commit()
    finally:
        conn.close()

    print("\nTODO OK -- filas y mensajes de prueba borrados.")


if __name__ == '__main__':
    asyncio.run(main())
