from contextlib import aclosing
from twscrape import API, ConnectError, NetworkError
from twscrape.models import parse_tweets
from twscrape.queue_client import XClIdGenStore
from flask import Flask, jsonify, request
from waitress import serve
import asyncio
import os

from estado_db import EstadoDB

app = Flask(__name__)


class ClienteX:
    """Encapsula el acceso a X (Twitter) vía twscrape: la cuenta logueada y la detección de
    tweets nuevos de las cuentas vigiladas (CUENTAS), que es lo que expone la ruta /check. Una
    sola instancia por proceso (ver `cliente_x` más abajo), creada al importar el módulo.
    """

    CUENTAS = ['CriptoNorber', 'CriptoNoticias', 'CriptoTendencia', 'CryptocapoOO']

    # Páginas de UserTweets (~20 tweets cada una) que se recorren como máximo buscando since_id.
    # Con chequeos cada 3 min alcanza la primera; las demás solo se piden para ponerse al día
    # después de un corte, y cada página es una request más contra el rate limit de la cuenta.
    MAX_PAGINAS = 3

    # Por debajo de los 60 s del HTTP Request de n8n: si twscrape se cuelga (por ejemplo,
    # esperando que se libere una cuenta con rate limit) la consulta se cancela acá y el hilo de
    # Flask termina, en vez de seguir vivo después de que n8n cortó la conexión.
    TIMEOUT_CHECK = 45

    # Timeout para /publicar -- una sola llamada de escritura a X (a diferencia de /check, que
    # puede recorrer varias páginas), así que alcanza con menos margen que TIMEOUT_CHECK.
    TIMEOUT_PUBLICAR = 30

    # Endpoint interno (no documentado) que usa el cliente web de X para publicar un tweet -- no
    # hay API oficial configurada (ver README), así que se reusa la sesión ya logueada de
    # twscrape para mandar la misma request que manda el navegador. queryId capturado en vivo
    # contra la cuenta real el 13/09 interceptando la request real del navegador (no se confió en
    # la referencia de una librería de terceros sin probarla -- esa referencia resultó tener un
    # queryId distinto, ya desactualizado). Como esto es un endpoint interno, X puede rotar este
    # id sin aviso: publicar_tweet() nunca asume éxito ante una respuesta que no matchee
    # exactamente el shape esperado (ver ahí el criterio real/error/ambiguo).
    OP_CREATE_TWEET = 'CUWCG7oBfrG71ZUXUtpwbw/CreateTweet'

    # Bloque de "features" tal cual lo manda el cliente web real al publicar (capturado en la
    # misma request de arriba) -- son flags de qué funcionalidades tiene habilitadas esa versión
    # del sitio, sin relación con el contenido del tweet en sí. Se mandan igual porque no hay
    # forma de saber cuáles X exige realmente sin mandarlas todas como las manda el navegador.
    FEATURES_CREATE_TWEET = {
        'premium_content_api_read_enabled': False,
        'communities_web_enable_tweet_community_results_fetch': True,
        'c9s_tweet_anatomy_moderator_badge_enabled': True,
        'responsive_web_grok_analyze_button_fetch_trends_enabled': False,
        'responsive_web_grok_analyze_post_followups_enabled': True,
        'rweb_cashtags_composer_attachment_enabled': True,
        'responsive_web_jetfuel_frame': True,
        'rweb_sports_post_context_enabled': False,
        'responsive_web_grok_share_attachment_enabled': True,
        'responsive_web_grok_annotations_enabled': True,
        'responsive_web_edit_tweet_api_enabled': True,
        'rweb_conversational_replies_downvote_enabled': False,
        'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
        'view_counts_everywhere_api_enabled': True,
        'longform_notetweets_consumption_enabled': True,
        'responsive_web_twitter_article_tweet_consumption_enabled': True,
        'content_disclosure_indicator_enabled': True,
        'content_disclosure_ai_generated_indicator_enabled': True,
        'responsive_web_grok_show_grok_translated_post': True,
        'responsive_web_grok_analysis_button_from_backend': True,
        'post_ctas_fetch_enabled': False,
        'longform_notetweets_rich_text_read_enabled': True,
        'longform_notetweets_inline_media_enabled': False,
        'profile_label_improvements_pcf_label_in_post_enabled': True,
        'responsive_web_profile_redirect_enabled': True,
        'rweb_tipjar_consumption_enabled': False,
        'verified_phone_label_enabled': False,
        'articles_preview_enabled': True,
        'rweb_cashtags_enabled': True,
        'responsive_web_grok_community_note_auto_translation_is_enabled': True,
        'freedom_of_speech_not_reach_fetch_enabled': True,
        'standardized_nudges_misinfo': True,
        'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
        'responsive_web_grok_image_annotation_enabled': True,
        'responsive_web_grok_imagine_annotation_enabled': True,
        'responsive_web_graphql_timeline_navigation_enabled': True,
    }

    def __init__(self):
        # os.environ[...] sin default: si falta alguna, se prefiere que el proceso no arranque
        # (KeyError ruidoso al importar el módulo) antes que arrancar a medias sin poder loguear
        # en X.
        self.username = os.environ['USERNAME']
        self.email = os.environ['EMAIL']
        self.password = os.environ['PASSWORD']
        self.cookies = os.environ['COOKIES']
        self.api = API()  # usa accounts.db por defecto

    async def setup(self):
        await self.api.pool.add_account(
            self.username, self.password, self.email, self.email, cookies=self.cookies,
        )
        await self.api.pool.login_all()

    async def tweets_nuevos(self, user, since_id):
        """Tweets propios de la cuenta con id > since_id, del más viejo al más nuevo.

        Sin since_id devuelve solo el más reciente. incompleto=True si se agotaron las
        MAX_PAGINAS sin llegar a since_id: puede haber tweets nuevos anteriores al primero
        devuelto que no se trajeron.
        """
        propios = {}
        llego_a_since_id = False
        incompleto = False
        paginas_leidas = 0
        async with aclosing(self.api.user_tweets_raw(user.id)) as paginas:
            async for rep in paginas:
                paginas_leidas += 1
                for tweet in parse_tweets(rep.json()):
                    # La respuesta también trae los tweets citados, que son de otros autores.
                    if tweet.user.id != user.id:
                        continue
                    propios[tweet.id] = tweet
                    # El fijado aparece primero aunque sea viejo: no indica que llegamos a since_id.
                    if since_id is not None and tweet.id <= since_id and tweet.id not in user.pinnedIds:
                        llego_a_since_id = True
                if since_id is None or llego_a_since_id:
                    break
                if paginas_leidas >= self.MAX_PAGINAS:
                    incompleto = True
                    break

        ordenados = sorted(propios.values(), key=lambda t: t.id)
        if since_id is None:
            return ordenados[-1:], False
        return [t for t in ordenados if t.id > since_id], incompleto

    @staticmethod
    def formatear(tweet):
        imagen_url = None
        if tweet.media and tweet.media.photos:
            imagen_url = tweet.media.photos[0].url
        return {
            'texto': tweet.rawContent,
            'id': str(tweet.id),
            'fecha': str(tweet.date),
            'imagen': imagen_url,
        }

    @staticmethod
    def validar_since_id(valor):
        """Normaliza el since_id de una cuenta recibido en el body.

        None (cuenta ausente del body, o sin fila guardada todavía) es válido y pide línea base.
        Cualquier otra cosa que no sea un string numérico es un since_id inválido.
        """
        if valor is None:
            return None, None
        if not (isinstance(valor, str) and valor.isascii() and valor.isdigit()):
            return None, f'since_id inválido: {valor!r}'
        return int(valor), None

    async def get_latest_tweets(self, since_ids):
        resultados = {}
        for cuenta in self.CUENTAS:
            since_id, error = self.validar_since_id(since_ids.get(cuenta))
            if error:
                print(f"ERROR con {cuenta}: {error}")
                resultados[cuenta] = {'error': error}
                continue

            try:
                user = await self.api.user_by_login(cuenta)
                tweets, incompleto = await self.tweets_nuevos(user, since_id)
                aviso = " (incompleto: no se llegó a since_id)" if incompleto else ""
                print(f"{cuenta}: {len(tweets)} tweets nuevos desde {since_id}{aviso}")
                resultados[cuenta] = {
                    'tweets': [self.formatear(t) for t in tweets],
                    'incompleto': incompleto,
                }
            except Exception as e:
                print(f"ERROR con {cuenta}: {e}")
                resultados[cuenta] = {'error': str(e)}

            await asyncio.sleep(2)

        return resultados

    async def publicar_tweet(self, texto):
        """Publica `texto` como un tweet real en X, reusando la sesión ya logueada de twscrape
        (misma cuenta que usa el resto de esta clase para leer) -- no se agrega ninguna librería
        de publicación (twikit ni ninguna otra): account.make_client() ya arma los headers de
        auth/csrf/cookies, y XClIdGenStore ya genera el x-client-transaction-id que exige X en
        cada request, exactamente igual que hace Ctx.req de twscrape para sus propias lecturas.

        Devuelve siempre un dict con 'resultado' -- nunca levanta para un fallo de X o de red, así
        que quien llama (la ruta /publicar) nunca tiene que inferir nada del status HTTP:
        - {'resultado': 'real', 'id': ..., 'url': ...} -- éxito confirmado (rest_id presente).
        - {'resultado': 'error', 'detalle': ...} -- se sabe con certeza que NO se publicó
          (conexión rechazada antes de mandar nada, o X contestó completo con un rechazo
          explícito). Retryable.
        - {'resultado': 'ambiguo', 'detalle': ...} -- no se sabe si se publicó o no (timeout o
          corte de red DESPUÉS de mandar la request, o una respuesta que no matchea ni éxito ni
          rechazo -- por ejemplo si X rotó OP_CREATE_TWEET/FEATURES_CREATE_TWEET). NO retryable
          solo: podría estar publicando el mismo texto dos veces.
        """
        account = await self.api.pool.get_account(self.username)
        if account is None:
            return {'resultado': 'error', 'detalle': f'cuenta {self.username!r} no está en accounts.db'}

        client = account.make_client()
        try:
            path = f'/i/api/graphql/{self.OP_CREATE_TWEET}'

            try:
                # XClIdGenStore.get() no es una simple lectura de caché la primera vez que se pide
                # para esta cuenta: XClIdGen.create() (twscrape/xclid.py) hace requests HTTP reales
                # contra x.com para sacar las claves con las que arma el x-client-transaction-id, y
                # parsea el HTML/JS que devuelve -- puede fallar por red (igual que client.post más
                # abajo) o porque X cambió la página y el parseo de twscrape quedó desactualizado
                # (XClIdParseError/XClIdAccountError). Nada de esto llegó a mandar todavía ninguna
                # request a CreateTweet, así que se sabe con certeza que no se publicó -- es
                # 'error' (retryable), nunca 'ambiguo', sea cual sea la excepción puntual.
                gen = await XClIdGenStore.get(account.username, cookies=account.cookies)
                headers = {'x-client-transaction-id': gen.calc('POST', path)}
            except Exception as e:
                return {
                    'resultado': 'error',
                    'detalle': f'no se pudo generar x-client-transaction-id: {e!r}',
                }

            body = {
                'variables': {
                    'tweet_text': texto,
                    'media': {'media_entities': [], 'possibly_sensitive': False},
                    'semantic_annotation_ids': [],
                },
                'features': self.FEATURES_CREATE_TWEET,
                'queryId': self.OP_CREATE_TWEET.split('/')[0],
            }

            try:
                rep = await client.post(f'https://x.com{path}', json=body, headers=headers)
            except ConnectError as e:
                return {'resultado': 'error', 'detalle': f'no se pudo conectar a X: {e}'}
            except NetworkError as e:
                return {'resultado': 'ambiguo', 'detalle': f'sin respuesta de X (timeout/corte): {e}'}
            except Exception as e:
                # Red de seguridad: cualquier excepción de bajo nivel que twscrape no reclasifique
                # como ConnectError/NetworkError (protocolo, decodificación, etc.) -- a esta altura
                # la request a CreateTweet puede haber salido, así que el criterio seguro es
                # 'ambiguo', igual que un timeout, nunca 'error' ni 'real'.
                return {
                    'resultado': 'ambiguo',
                    'detalle': f'excepción inesperada mandando la request a X: {e!r}',
                }

            try:
                datos = rep.json()
            except Exception:
                return {
                    'resultado': 'ambiguo',
                    'detalle': f'respuesta no-JSON de X (status {rep.status_code}): {rep.text[:200]!r}',
                }

            tweet_id = None
            if isinstance(datos, dict):
                try:
                    tweet_id = datos['data']['create_tweet']['tweet_results']['result']['rest_id']
                except (KeyError, TypeError):
                    tweet_id = None

            if tweet_id:
                return {
                    'resultado': 'real',
                    'id': str(tweet_id),
                    'url': f'https://x.com/{self.username}/status/{tweet_id}',
                }

            if isinstance(datos, dict) and datos.get('errors'):
                return {'resultado': 'error', 'detalle': str(datos['errors'])[:500]}

            # Ni rest_id ni errors: no matchea el shape esperado (posible rotación del queryId o
            # de las features) -- no se puede asumir ni éxito ni rechazo.
            return {
                'resultado': 'ambiguo',
                'detalle': f'respuesta inesperada de X (status {rep.status_code}): {str(datos)[:300]!r}',
            }
        finally:
            await client.aclose()


cliente_x = ClienteX()

# Separada de accounts.db (esa es de twscrape): acá vive el estado de la conversación del bot
# (elegir/editar/publicar un tweet), que se está migrando de n8n a Python paso a paso.
estado_db = EstadoDB()


@app.route('/borradores', methods=['POST'])
def crear_borrador():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'body inválido: se espera JSON'}), 400

    tweet_id = body.get('tweet_id')
    cuenta = body.get('cuenta')
    texto = body.get('texto')
    fecha = body.get('fecha')

    faltantes = [campo for campo, valor in
                 [('tweet_id', tweet_id), ('cuenta', cuenta), ('texto', texto), ('fecha', fecha)]
                 if valor is None or valor == '']
    if faltantes:
        return jsonify({'error': f'faltan campos: {", ".join(faltantes)}'}), 400
    if not isinstance(texto, str):
        return jsonify({'error': 'texto debe ser string'}), 400

    tweet_id = str(tweet_id)
    cuenta = str(cuenta)
    fecha = str(fecha)

    nuevo_id, creado = estado_db.crear_borrador(tweet_id, cuenta, texto, fecha)
    return jsonify({'id': nuevo_id}), 201 if creado else 200


@app.route('/borradores/<tweet_id>', methods=['GET'])
def obtener_borrador(tweet_id):
    borrador = estado_db.obtener_borrador_por_tweet_id(tweet_id)
    if borrador is None:
        return jsonify({'error': 'no hay borrador para ese tweet_id'}), 404
    return jsonify(borrador)


@app.route('/check', methods=['POST'])
def check():
    # Body: {cuenta: since_id, ...} con un since_id por cuenta (o null si no hay historial
    # guardado todavía). Una sola llamada para todas las cuentas de ClienteX.CUENTAS:
    # get_latest_tweets ya espacía las consultas a X con sleep(2), así que pedirlas juntas reparte
    # mejor la carga contra el rate limit que si el llamador dispara un pedido HTTP por cuenta.
    since_ids = request.get_json(silent=True)
    if not isinstance(since_ids, dict):
        return jsonify({'error': 'body inválido: se espera un JSON {cuenta: since_id}'}), 400

    try:
        resultados = asyncio.run(
            asyncio.wait_for(cliente_x.get_latest_tweets(since_ids), ClienteX.TIMEOUT_CHECK)
        )
    except TimeoutError:
        print(f"ERROR: /check cancelado a los {ClienteX.TIMEOUT_CHECK} s")
        return jsonify({'error': f'twscrape no respondió en {ClienteX.TIMEOUT_CHECK} s'}), 504
    return jsonify(resultados)


@app.route('/publicar', methods=['POST'])
def publicar():
    # Body: {"texto": "..."}. Siempre devuelve 200 con un campo 'resultado' -- ver
    # ClienteX.publicar_tweet para las tres formas que puede tomar (real/error/ambiguo). Quien
    # llama (bot_telegram.py) no debe inferir nada del status HTTP, solo mirar 'resultado'.
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get('texto'), str) or not body['texto']:
        return jsonify({'error': 'body inválido: se espera JSON {"texto": "..."}'}), 400

    try:
        resultado = asyncio.run(
            asyncio.wait_for(cliente_x.publicar_tweet(body['texto']), ClienteX.TIMEOUT_PUBLICAR)
        )
    except TimeoutError:
        print(f"ERROR: /publicar cancelado a los {ClienteX.TIMEOUT_PUBLICAR} s")
        # Se mandó la request a X y no hubo tiempo de ver la respuesta -- no se sabe si publicó.
        return jsonify({
            'resultado': 'ambiguo',
            'detalle': f'sin respuesta de X en {ClienteX.TIMEOUT_PUBLICAR} s',
        })
    return jsonify(resultado)


if __name__ == '__main__':
    estado_db.crear_tablas_borradores()
    asyncio.run(cliente_x.setup())
    serve(app, host='0.0.0.0', port=5000)
