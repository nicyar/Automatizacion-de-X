"""Acceso compartido a estado.db (SQLite) entre bot_x.py y bot_telegram.py.

Antes de este refactor, cada archivo tenía su propia copia (idéntica) de get_db()/init_db() y sus
propias funciones sueltas para leer y escribir borradores/publicaciones/ultimos_tweets. Este
módulo junta esas operaciones en una sola clase (EstadoDB) para no mantener la misma lógica de
conexión y las mismas sentencias SQL duplicadas en dos archivos.

Cada proceso (bot-tweets y telegram-poller, ver docker-compose.yml) instancia su propio EstadoDB
apuntando al mismo ESTADO_DB_PATH -- no comparten la instancia de Python, comparten el archivo en
disco. SQLite en modo WAL (ver _conectar) es lo que permite que ambos, corriendo en contenedores
distintos con estado.db montado por bind mount, lean y escriban ese archivo al mismo tiempo sin
pisarse.
"""
import os
import secrets
import sqlite3
from datetime import datetime, timezone

ESTADO_DB_PATH = os.environ.get('ESTADO_DB_PATH', 'datos/estado.db')


def ahora_iso():
    return datetime.now(timezone.utc).isoformat()


class EstadoDB:
    """Operaciones sobre estado.db: borradores (un renglón por tweet en proceso de publicación),
    publicaciones (cerrojo contra publicar dos veces el mismo borrador) y ultimos_tweets (el
    since_id por cuenta que reemplaza a la Data Table de n8n).

    Cada método abre y cierra su propia conexión en vez de mantener una persistente -- mismo
    patrón que las funciones sueltas que reemplaza. Para el volumen de este sistema (unos pocos
    borradores por día) no vale la pena la complejidad de compartir una conexión entre pedidos.
    """

    def __init__(self, path=None):
        self.path = path or ESTADO_DB_PATH

    def _conectar(self):
        # timeout=15: cuánto espera sqlite3 antes de levantar "database is locked" si otro proceso
        # (bot-tweets o telegram-poller) tiene una transacción de escritura abierta en este
        # instante -- más que el default de 5 s del propio sqlite3.connect(), con margen extra para
        # el bind mount de Docker Desktop en Windows (más lento que un disco nativo de Linux). Para
        # el volumen de este sistema (unos pocos borradores por día) el costo de esperar unos
        # segundos de más ante una colisión real es nulo comparado con el de levantar una excepción
        # a mitad del candado de publicación (tomar_candado_publicacion/resolver_publicacion).
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        # WAL en vez del rollback journal por defecto: estado.db se comparte por bind mount entre
        # bot-tweets y telegram-poller (dos procesos, dos contenedores), y WAL es lo que permite
        # que un writer y varios readers convivan sin pisarse entre procesos distintos.
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    # --- Creación de tablas ---

    def crear_tablas_borradores(self):
        """Crea borradores y publicaciones si no existen. La llaman ambos procesos al arrancar,
        porque docker-compose no fuerza un orden entre bot-tweets y telegram-poller: cualquiera de
        los dos puede ser el primero en necesitar las tablas."""
        conn = self._conectar()
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS borradores (
                    id TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    card_message_id INTEGER,
                    tweet_id TEXT UNIQUE,
                    cuenta TEXT,
                    tweet_texto TEXT,
                    tweet_fecha TEXT,
                    opciones TEXT,
                    elegida INTEGER,
                    texto_final TEXT,
                    paso TEXT,
                    prompt_message_id INTEGER,
                    creado_en TEXT,
                    actualizado_en TEXT
                )
            ''')
            # Es el cerrojo contra doble publicación que usa ConversacionTelegram.manejar_pub.
            # bot_x.py no la usa todavía (no hay integración real con X), pero la crea igual por
            # si arranca antes que telegram-poller.
            conn.execute('''
                CREATE TABLE IF NOT EXISTS publicaciones (
                    borrador_id TEXT PRIMARY KEY,
                    estado TEXT,
                    x_tweet_id TEXT,
                    creado_en TEXT
                )
            ''')
            conn.commit()
        finally:
            conn.close()

    def crear_tabla_ultimos_tweets(self):
        """Reemplaza a la Data Table "ultimos_tweets" de n8n: guarda el since_id (el id del tweet
        más nuevo ya avisado) por cuenta. Solo la necesita el proceso de Telegram
        (DetectorTweetsNuevos) -- bot_x.py no lee ni escribe since_id, lo recibe como parámetro de
        quien lo llama -- así que bot_x.py nunca llama a este método."""
        conn = self._conectar()
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS ultimos_tweets (
                    cuenta TEXT PRIMARY KEY,
                    ultimo_id TEXT
                )
            ''')
            conn.commit()
        finally:
            conn.close()

    # --- Borradores ---

    @staticmethod
    def generar_id_borrador():
        # 8 caracteres hex: corto y apto para viajar en un callback_data de Telegram (límite 64 bytes).
        return secrets.token_hex(4)

    def crear_borrador(self, tweet_id, cuenta, texto, fecha):
        """Crea un borrador nuevo para tweet_id, o devuelve el existente si ya había uno
        (idempotente: dos pedidos casi simultáneos para el mismo tweet_id nunca crean dos filas).

        Devuelve (id_borrador, creado) -- creado es True solo si esta llamada insertó la fila
        (para que el llamador HTTP pueda devolver 201 vs 200, igual que antes del refactor).
        """
        conn = self._conectar()
        try:
            existente = conn.execute(
                'SELECT id FROM borradores WHERE tweet_id = ?', (tweet_id,)
            ).fetchone()
            if existente:
                return existente['id'], False

            ahora = ahora_iso()
            nuevo_id = self.generar_id_borrador()
            try:
                conn.execute(
                    '''INSERT INTO borradores
                       (id, tweet_id, cuenta, tweet_texto, tweet_fecha, paso, creado_en, actualizado_en)
                       VALUES (?, ?, ?, ?, ?, 'nuevo', ?, ?)''',
                    (nuevo_id, tweet_id, cuenta, texto, fecha, ahora, ahora)
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # Carrera contra otra request para el mismo tweet_id: la que ganó ya insertó.
                conn.rollback()
                existente = conn.execute(
                    'SELECT id FROM borradores WHERE tweet_id = ?', (tweet_id,)
                ).fetchone()
                return existente['id'], False

            return nuevo_id, True
        finally:
            conn.close()

    def obtener_borrador_por_tweet_id(self, tweet_id):
        conn = self._conectar()
        try:
            row = conn.execute(
                'SELECT * FROM borradores WHERE tweet_id = ?', (tweet_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def obtener_borrador(self, id_borrador):
        conn = self._conectar()
        try:
            row = conn.execute(
                'SELECT * FROM borradores WHERE id = ?', (id_borrador,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def obtener_borrador_esperando_texto(self, chat_id, prompt_message_id):
        """El borrador (si hay uno) al que `mensaje` responde: el que tiene ese chat_id y
        prompt_message_id como reply pendiente y todavía está en paso 'esperando_texto'. Usado por
        on_reply para distinguir un reply válido de un mensaje suelto."""
        conn = self._conectar()
        try:
            row = conn.execute(
                "SELECT * FROM borradores WHERE chat_id = ? AND prompt_message_id = ? "
                "AND paso = 'esperando_texto'",
                (chat_id, prompt_message_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def marcar_esperando_texto(self, id_borrador, chat_id, card_message_id, prompt_message_id, opciones_json):
        conn = self._conectar()
        try:
            conn.execute(
                "UPDATE borradores SET chat_id=?, card_message_id=?, prompt_message_id=?, opciones=?, "
                "paso='esperando_texto', actualizado_en=? WHERE id=?",
                (chat_id, card_message_id, prompt_message_id, opciones_json, ahora_iso(), id_borrador),
            )
            conn.commit()
        finally:
            conn.close()

    def actualizar_prompt_message_id(self, id_borrador, prompt_message_id):
        conn = self._conectar()
        try:
            conn.execute(
                "UPDATE borradores SET prompt_message_id=?, actualizado_en=? WHERE id=?",
                (prompt_message_id, ahora_iso(), id_borrador),
            )
            conn.commit()
        finally:
            conn.close()

    def marcar_confirmando(self, id_borrador, texto_final, elegida):
        conn = self._conectar()
        try:
            conn.execute(
                "UPDATE borradores SET texto_final=?, elegida=?, paso='confirmando', actualizado_en=? "
                "WHERE id=?",
                (texto_final, elegida, ahora_iso(), id_borrador),
            )
            conn.commit()
        finally:
            conn.close()

    def marcar_cancelado(self, id_borrador):
        conn = self._conectar()
        try:
            conn.execute(
                "UPDATE borradores SET paso='cancelado', actualizado_en=? WHERE id=?",
                (ahora_iso(), id_borrador),
            )
            conn.commit()
        finally:
            conn.close()

    def registrar_publicacion(self, id_borrador):
        """Intenta tomar el cerrojo de publicación de id_borrador insertando en publicaciones. Ese
        INSERT (borrador_id es PRIMARY KEY) es el candado real: aunque dos toques de "Publicar"
        lleguen casi al mismo tiempo, solo uno puede ganarlo.

        Devuelve True si esta llamada ganó el cerrojo -- insertó la fila en publicaciones y dejó
        borradores.paso en 'publicado_simulado' sin condición. Devuelve False si ya estaba
        publicado (otra llamada ganó antes); en ese caso también deja paso en
        'publicado_simulado', pero solo si todavía no lo estaba, para no pisar actualizado_en de
        una carrera que ya se resolvió.
        """
        ahora = ahora_iso()
        conn = self._conectar()
        try:
            try:
                conn.execute(
                    "INSERT INTO publicaciones (borrador_id, estado, creado_en) VALUES (?, 'simulado', ?)",
                    (id_borrador, ahora),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                conn.execute(
                    "UPDATE borradores SET paso='publicado_simulado', actualizado_en=? "
                    "WHERE id=? AND paso != 'publicado_simulado'",
                    (ahora, id_borrador),
                )
                conn.commit()
                return False

            # El INSERT de arriba es el cerrojo real -- ya tomó la publicación (simulada), y
            # ningún toque futuro puede volver a publicar este borrador. El paso se persiste
            # directo en publicado_simulado (sin pasar por un 'publicando' guardado): si algo
            # falla después de esto (por ejemplo, editar el mensaje de Telegram), el borrador no
            # queda trabado sin teclado y sin forma de reintentar.
            conn.execute(
                "UPDATE borradores SET paso='publicado_simulado', actualizado_en=? WHERE id=?",
                (ahora, id_borrador),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def tomar_candado_publicacion(self, id_borrador):
        """Candado de publicación real (a diferencia de registrar_publicacion, que es el de la
        publicación simulada -- ver MODO_PUBLICACION en bot_telegram.py). Devuelve True si esta
        llamada ganó el candado -- puede ganarlo insertando la fila (primera vez) o reclamando una
        fila que ya había quedado en 'error' (falla limpia anterior, se reintenta) -- y en ese
        caso deja borradores.paso en 'publicando' como señal defensiva (si el proceso muere justo
        acá, el mensaje visible y el estado en DB cuentan la misma historia).

        Devuelve False si el candado ya está tomado por otra llamada: en 'publicando' (una
        publicación real en curso, o que murió a mitad de camino -- nunca se reclama sola, sería
        arriesgar una publicación duplicada), 'real' (ya se publicó) o 'ambiguo' (no se sabe si
        salió, requiere revisión manual antes de reintentar)."""
        ahora = ahora_iso()
        conn = self._conectar()
        try:
            try:
                conn.execute(
                    "INSERT INTO publicaciones (borrador_id, estado, creado_en) VALUES (?, 'publicando', ?)",
                    (id_borrador, ahora),
                )
                gano = True
            except sqlite3.IntegrityError:
                conn.rollback()
                cur = conn.execute(
                    "UPDATE publicaciones SET estado='publicando', creado_en=? "
                    "WHERE borrador_id=? AND estado='error'",
                    (ahora, id_borrador),
                )
                gano = cur.rowcount > 0

            if gano:
                conn.execute(
                    "UPDATE borradores SET paso='publicando', actualizado_en=? WHERE id=?",
                    (ahora, id_borrador),
                )
            conn.commit()
            return gano
        finally:
            conn.close()

    def resolver_publicacion(self, id_borrador, estado, x_tweet_id=None):
        """Cierra el candado que tomó tomar_candado_publicacion, con el resultado real de haber
        intentado publicar contra X:

        - 'real': éxito confirmado -- guarda x_tweet_id y borradores.paso pasa a 'publicado'.
        - 'error': falla limpia (se sabe con certeza que no se publicó) -- libera el candado
          (vuelve a dejarlo en 'error', reclamable por un reintento) y borradores.paso vuelve a
          'confirmando' para que la tarjeta de Publicar/Editar/Descartar quede disponible de
          nuevo.
        - 'ambiguo': no se pudo confirmar si se publicó o no (timeout, corte de red a mitad de la
          request) -- borradores.paso pasa a 'revision_manual' y el candado NO se libera: no hay
          forma de reintentar sola sin arriesgar publicar el mismo texto dos veces, hace falta
          revisar el perfil de X a mano."""
        paso_por_estado = {'real': 'publicado', 'error': 'confirmando', 'ambiguo': 'revision_manual'}
        ahora = ahora_iso()
        conn = self._conectar()
        try:
            conn.execute(
                "UPDATE publicaciones SET estado=?, x_tweet_id=? WHERE borrador_id=?",
                (estado, x_tweet_id, id_borrador),
            )
            conn.execute(
                "UPDATE borradores SET paso=?, actualizado_en=? WHERE id=?",
                (paso_por_estado[estado], ahora, id_borrador),
            )
            conn.commit()
        finally:
            conn.close()

    # --- ultimos_tweets (since_id por cuenta) ---

    def leer_since_ids(self):
        """Todas las filas de ultimos_tweets como {cuenta: ultimo_id}. Una cuenta sin fila
        todavía (primer chequeo de esa cuenta) queda ausente del dict -- /check en bot_x.py ya
        trata una cuenta ausente del body como since_id nulo (línea base), mismo contrato que
        cumplía el nodo "Armar since_ids" de n8n con lo que traía "Get row(s)"."""
        conn = self._conectar()
        try:
            filas = conn.execute('SELECT cuenta, ultimo_id FROM ultimos_tweets').fetchall()
        finally:
            conn.close()
        return {fila['cuenta']: fila['ultimo_id'] for fila in filas}

    def actualizar_since_id(self, cuenta, ultimo_id):
        """Guarda ultimo_id tal cual llega (string) -- nunca pasa por int()/float(). Un id de
        tweet es un entero de hasta 19+ dígitos: convertirlo a int/float en el camino (incluso
        para ida y vuelta) arriesga perder precisión y guardar un since_id que ya no es carácter
        por carácter el original (bot_x.py sí lo vuelve a convertir a int, pero recién ahí, para
        comparar -- ver ClienteX.validar_since_id)."""
        conn = self._conectar()
        try:
            conn.execute(
                'INSERT INTO ultimos_tweets (cuenta, ultimo_id) VALUES (?, ?) '
                'ON CONFLICT(cuenta) DO UPDATE SET ultimo_id = excluded.ultimo_id',
                (cuenta, ultimo_id),
            )
            conn.commit()
        finally:
            conn.close()
