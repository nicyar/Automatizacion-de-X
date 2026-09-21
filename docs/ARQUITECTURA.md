# Arquitectura y guía de operación

Documentación técnica del sistema. Para una presentación general del proyecto, ver el [README](../README.md).

Son dos programas de Python que corren todo el tiempo dentro de Docker, más un módulo compartido:

- **`bot_x.py`** — habla con X (Twitter). Expone un mini servidor web interno (`/check`, `/borradores`, `/publicar`) que usa `bot_telegram.py` para detectar tweets nuevos, crear sus borradores y publicar el texto final.
- **`bot_telegram.py`** — habla con Telegram. Detecta los tweets nuevos (chequeo cada 3 minutos), arma las tarjetas, genera las propuestas de reescritura con IA (Gemini) y procesa los botones que se tocan y las respuestas que se escriben.
- **`estado_db.py`** — el código de acceso a `datos/estado.db` (SQLite), compartido por los dos programas para saber en qué paso está cada borrador.

---

## 1. Cómo está organizado el código

El sistema quedó reorganizado en clases, una por responsabilidad real. Cada clase se instancia una sola vez por proceso (un "singleton" de hecho, sin librería para eso) y esa instancia es la que usan el resto de las funciones del archivo.

### `estado_db.py` — acceso a la base de datos

- **`EstadoDB`** — la única clase que sabe hablar con `estado.db`. Encapsula:
  - **Conexión**: `_conectar()` abre una conexión nueva por operación (no mantiene una persistente — para el volumen de este sistema, unos pocos borradores por día, no hace falta más) y activa el modo WAL de SQLite, que es lo que permite que `bot_x.py` y `bot_telegram.py` (dos procesos, dos contenedores) lean y escriban el mismo archivo al mismo tiempo sin pisarse.
  - **Creación de tablas**: `crear_tablas_borradores()` crea `borradores` (un renglón por cada tweet en proceso) y `publicaciones` (el candado contra publicar el mismo borrador dos veces). `crear_tabla_ultimos_tweets()` crea la tabla que guarda el último tweet avisado por cuenta (el since_id) — solo la usa `bot_telegram.py`.
  - **Borradores**: `crear_borrador(...)` (crea uno nuevo o devuelve el existente si ya había uno para ese tweet, sin duplicar), `obtener_borrador(id)` / `obtener_borrador_por_tweet_id(...)` / `obtener_borrador_esperando_texto(...)` (las tres formas en que el resto del código necesita buscar un borrador), y una serie de métodos `marcar_*`/`actualizar_*` que mueven un borrador de un paso al siguiente.
  - **Publicaciones**: `registrar_publicacion(id_borrador)` — el método que implementa el candado real contra la doble publicación (ver más abajo, en `ConversacionTelegram.manejar_pub`).
  - **`ultimos_tweets`**: `leer_since_ids()` / `actualizar_since_id(...)`.

  Cada proceso crea su propia instancia (`estado_db = EstadoDB()`), pero las dos apuntan al mismo archivo — no comparten el objeto de Python, comparten el archivo en disco.

### `bot_x.py` — detección contra X (Twitter)

- **`ClienteX`** — encapsula la cuenta de X logueada (vía `twscrape`) y la detección de tweets nuevos:
  - `setup()` — al arrancar, carga la cuenta guardada (usuario, contraseña, cookies) y la deja lista para usarse.
  - `tweets_nuevos(user, since_id)` — el corazón de la detección. Le pide a X los tweets más recientes de una cuenta y devuelve solo los más nuevos que `since_id`. Si no llegó a encontrar ese último tweet conocido en las páginas que revisó, avisa que la lista puede estar incompleta (`incompleto=True`) en vez de asumir que no hay nada más.
  - `formatear(tweet)` — convierte un tweet de `twscrape` a un diccionario simple (texto/id/fecha/imagen) listo para mandar por HTTP.
  - `validar_since_id(valor)` — revisa que el "último tweet visto" que llega en el pedido sea válido, antes de usarlo.
  - `get_latest_tweets(since_ids)` — recorre las cuentas vigiladas (`ClienteX.CUENTAS`), pide los tweets nuevos de cada una y arma el resultado combinado, con una pausa de 2 segundos entre cuenta y cuenta para no golpear de una el límite de pedidos de X.
  - `publicar_tweet(texto)` — publica `texto` como un tweet real. No hay API oficial de X configurada (no hace falta: ver sección 3), así que reusa la misma sesión ya logueada que usa el resto de la clase para leer — `account.make_client()` de `twscrape` arma los headers de auth/csrf/cookies, y `XClIdGenStore` genera el `x-client-transaction-id` que X exige en cada request (el mismo mecanismo que usa `twscrape` puertas adentro para sus propias lecturas). Nunca levanta una excepción por un fallo de X o de red: siempre devuelve un dict con `resultado` ∈ `{real, error, ambiguo}` — ver el docstring del método para el criterio exacto de cada uno, es la pieza más delicada de todo el sistema.

  El archivo instancia un solo `cliente_x = ClienteX()` y lo usa desde las rutas de Flask:
  - **`POST /check`** — la puerta de entrada que consulta `bot_telegram.py` cada 3 minutos: recibe el último tweet visto de cada cuenta y devuelve los nuevos. Corta a los 45 segundos si X no contesta.
  - **`POST /borradores`** / **`GET /borradores/<tweet_id>`** — crean o consultan un borrador, delegando en `estado_db` (la instancia de `EstadoDB` de este proceso).
  - **`POST /publicar`** — recibe `{"texto": "..."}`, llama a `publicar_tweet` y devuelve siempre HTTP 200 con el dict de resultado (nunca hay que inferir nada del status HTTP, solo mirar el campo `resultado`).

### `bot_telegram.py` — la conversación con el usuario

Tres clases, cada una con una responsabilidad de las que ya existían en el flujo (detectar, generar propuestas, conversar), más un puñado de funciones sueltas que solo arman textos (no tienen estado propio, así que no había motivo para meterlas en una clase).

- **`ClienteGemini`** — genera las 3 propuestas de reescritura por IA:
  - `generar_propuestas(tweet_texto)` es el único punto de entrada que usa el resto del código: si hay `GEMINI_API_KEY` configurada llama a la IA real (`generar_propuestas_ia`); si no, devuelve propuestas de prueba (`_propuestas_stub`) para poder seguir probando el resto de la conversación sin la key cargada.
  - Si la llamada real a Gemini falla por cualquier motivo (cuota gratuita agotada -- 20 requests/día en el tier free, timeout, error de red) `procesar_tw` no traba al usuario con un error: cae a pedirle el texto del tweet directamente a mano, sin las 3 propuestas (ver `ConversacionTelegram.avanzar_a_esperando_texto` con `opciones=[]` y `texto_pedir_texto_manual`). El propio mensaje avisa el motivo. La cuota gratuita resetea a medianoche hora Pacífico.
  - Mantiene un único `genai.Client` por proceso (creado perezosamente en `_obtener_cliente`, en el primer uso real) en vez de crear uno nuevo por tweet.
  - Si alguna propuesta se pasa de 280 caracteres reintenta una vez (`generar_propuestas_ia`) y, si el reintento también se pasa, la recorta (`_recortar_a_280`).

- **`DetectorTweetsNuevos`** — el chequeo periódico de tweets nuevos y el primer aviso a Telegram (reemplaza al Schedule Trigger + el resto del workflow de n8n hasta "Guardar borrador"):
  - `chequeo_periodico(context)` — el job que corre cada `INTERVALO_CHEQUEO_SEGUNDOS` (3 minutos): lee los since_id guardados (vía `estado_db`), consulta `/check` en `bot-tweets` (`pedir_check`), arma los tweets nuevos de cada cuenta (`armar_items_nuevos`) y los avisa uno por uno.
  - `avisar_tweet_nuevo(context, client, item)` — crea o recupera el borrador (`crear_borrador_remoto`, que llama a `POST /borradores` en `bot-tweets`) **antes** de mandar el aviso, y recién con el id real del borrador arma el botón y manda el mensaje. Ese orden es el arreglo de un bug real que tuvo este sistema con n8n (ver el docstring del método para el detalle).
  - Si un aviso falla, `chequeo_periodico` corta ahí la tanda de esa cuenta (no avanza el since_id más allá del tweet que falló), para que se reintente entero en el próximo chequeo en vez de saltearlo para siempre.

- **`ConversacionTelegram`** — la máquina de estados de un borrador: `nuevo → esperando_texto → confirmando → publicado` (o `cancelado`). Con la publicación real hay además dos pasos posibles entre `confirmando` y `publicado`: `publicando` (transitorio, mientras se espera la respuesta de X) y `revision_manual` (si nunca se pudo confirmar si el tweet salió o no — ver `manejar_pub` más abajo). Con `MODO_PUBLICACION=mock` el paso final es `publicado_simulado` en vez de `publicado`, sin pasar por ninguno de los dos intermedios. Es el punto de entrada de todo lo que toca o escribe el usuario:
  - `on_callback(update, context)` — despacha según el prefijo del botón tocado (`tw:`, `pub:`, `edit:`, `desc:`) a `manejar_tw`/`manejar_pub`/`manejar_edit`/`manejar_desc`.
  - `on_reply(update, context)` — despacha las respuestas de texto: si son un reply al pedido de texto pendiente, a `procesar_texto_respuesta`; si es un reply directo al aviso original (sin tocar el botón), sigue el mismo camino que `manejar_tw`.
  - `sacar_boton(bot, chat_id, message_id)` — le saca el teclado a un mensaje ya procesado, apoyándose en que Telegram avisa si el teclado ya no estaba ("message is not modified") en vez de llevar una cuenta propia de quién tocó primero. La usan tanto la carrera del aviso (`tw:`) como la de la tarjeta de confirmación (`pub:`/`edit:`/`desc:`).
  - `validar_confirmando(query, context, id_borrador)` — el chequeo común a Publicar/Editar/Descartar: borrador existente, del chat correcto, en paso "confirmando", y que este toque haya ganado la carrera.
  - `avanzar_a_esperando_texto(...)` — manda el ForceReply pidiendo el texto y recién si eso sale bien persiste el avance de paso (si el envío falla, el borrador queda como estaba).
  - `procesar_texto_respuesta(...)` — valida el texto (no vacío, no más de 280 caracteres) y, si está bien, pasa a "confirmando" con la tarjeta de tres botones.
  - `manejar_pub(...)` — antes de nada compara la huella del texto (`calcular_hash6`) contra la que tenía el botón cuando se armó (si cambió mientras tanto, no publica). Si coincide, despacha según `MODO_PUBLICACION` a `_manejar_pub_mock` (comportamiento de siempre, vía `estado_db.registrar_publicacion`) o `_manejar_pub_real`:
    - `_manejar_pub_real` primero toma `estado_db.tomar_candado_publicacion(id_borrador)` — el candado real contra publicar dos veces el mismo borrador (un `INSERT` en `publicaciones`, igual que el simulado, pero con tres estados posibles en vez de uno: `publicando` → `real`/`error`/`ambiguo`). Recién con el candado ganado llama a `POST /publicar` en `bot-tweets` (`pedir_publicar`) y clasifica la respuesta con `estado_db.resolver_publicacion(...)`: `real` deja el borrador en `publicado` con el id real guardado; `error` (falla limpia, se sabe con certeza que no se publicó) libera el candado y vuelve a `confirmando` con el botón de Publicar disponible para reintentar; `ambiguo` (no se pudo confirmar si salió o no — timeout, corte de red) pasa a `revision_manual` **sin** liberar el candado, para no arriesgar una publicación duplicada — ese estado no se resuelve solo, hace falta revisar el perfil de X a mano y corregir la fila en `estado.db`.
  - `manejar_edit(...)` / `manejar_desc(...)` — "Seguir editando" (reofrece las mismas 3 propuestas, sin gastar otra llamada a la IA) y "Descartar".

  El archivo instancia `cliente_gemini = ClienteGemini()`, `detector_tweets = DetectorTweetsNuevos(estado_db, CHAT_ID)` y `conversacion = ConversacionTelegram(estado_db, cliente_gemini, CHAT_ID)`, y `main()` conecta esas instancias a la `Application` de python-telegram-bot (el job periódico y los dos handlers, de callback y de reply).

---

## 2. Revisión de eficiencia

En general el código está bien ajustado al volumen que maneja: cuatro cuentas de X, un chequeo cada 3 minutos, un solo chat de Telegram, y un puñado de tweets por día. No hay bucles anidados sobre listas grandes ni nada que vaya a degradarse con más uso a esta escala. Dos observaciones puntuales, ninguna urgente:

- **`bot_x.py`, método `ClienteX.get_latest_tweets` (`user = await self.api.user_by_login(cuenta)`)** — esto le pide a X el usuario completo (incluido su ID interno) en **cada** chequeo de cada cuenta, es decir cada 3 minutos, para siempre. El ID de una cuenta de X no cambia nunca. Se podría resolver una sola vez al arrancar (o guardarlo en memoria la primera vez que se pide) y evitar ese pedido de ahí en adelante. El propio código ya deja comentado que cada pedido a X consume parte del límite de la cuenta ("cada página es una request más contra el rate limit"), así que sacar este pedido de más libera margen para cuando haga falta pedir más páginas.
- **Conexión a la base de datos abierta y cerrada en cada pedido** (`EstadoDB._conectar()`, en `estado_db.py`) — no se reutiliza una conexión entre pedidos, se abre una nueva cada vez. Para el volumen actual (unos pocos borradores por día) esto no tiene ningún impacto medible; lo menciono solo para que quede registrado, no es algo que valga la pena tocar ahora.

No encontré consultas repetidas dentro de un mismo pedido, ni nada que se recalcule sin necesidad.

---

## 3. Publicación real vs. modo simulado

Por default (`MODO_PUBLICACION` sin definir en `.env`, o `MODO_PUBLICACION=real`) tocar "Publicar" en Telegram publica de verdad en X. Si en algún momento hace falta iterar sobre el resto de la conversación (elegir texto, editar, descartar) sin gastar tweets reales, se puede volver al comportamiento de siempre agregando a `.env`:

```
MODO_PUBLICACION=mock
```

y reconstruyendo los contenedores (`docker compose up -d --build`). En modo `mock`, `bot_telegram.py` ni siquiera le pega a `bot-tweets` para esto -- sigue exactamente el camino de antes (`estado_db.registrar_publicacion`), sin tocar X para nada.

### Por qué no hay una API oficial de X configurada

`.env` solo tiene las credenciales de la cuenta (usuario/contraseña/cookies) que usa `twscrape` para loguearse y leer tweets -- no hay claves de la API oficial de X (esa requiere anotarse como developer, y algunos niveles de publicación son pagos). En vez de eso, `ClienteX.publicar_tweet` reusa esa misma sesión ya logueada para mandar la misma request que manda el navegador cuando publicás un tweet a mano -- un endpoint interno de X (GraphQL, `CreateTweet`), no documentado oficialmente.

Esto tiene una consecuencia real: X puede cambiar ese endpoint (el `queryId` en `ClienteX.OP_CREATE_TWEET`, o las `features` que espera) sin avisar. Por diseño, `publicar_tweet` nunca asume que se publicó ante una respuesta que no matchea exactamente lo esperado -- devuelve `resultado: 'ambiguo'` en vez de `'real'` o `'error'`, y ese caso queda pausado en `revision_manual` esperando revisión manual (ver `ConversacionTelegram.manejar_pub` en la sección 1). Si eso empieza a pasar seguido, hay que volver a capturar la request real desde un navegador logueado con la cuenta (Network tab, buscar la llamada a `CreateTweet` al publicar un tweet a mano) y actualizar `OP_CREATE_TWEET`/`FEATURES_CREATE_TWEET` en `bot_x.py`.

---

## 4. Cómo correrlo

### Antes de arrancar

Necesitás tener **Docker Desktop** instalado y **abierto y corriendo** (el ícono de la ballena en la barra de tareas, sin el símbolo de "cargando"). Sin eso, ningún comando de los de abajo va a funcionar.

Todos los comandos se ejecutan desde una terminal (PowerShell), **parado en la carpeta del proyecto** (la que tiene el `docker-compose.yml`). Si abrís una terminal nueva, primero navegá ahí.

### Prender el sistema

```
docker compose up -d
```

Esto levanta los dos servicios (`bot-tweets` y `telegram-poller`) en segundo plano. La primera vez que lo corrés puede tardar un par de minutos porque construye la imagen; las siguientes veces es casi instantáneo.

### Apagar el sistema

```
docker compose stop
```

Apaga los contenedores pero no los borra — la próxima vez que hagas `docker compose up -d` arrancan de nuevo con todo como estaba. Si en algún momento querés borrarlos del todo (no hace falta para el uso normal), es `docker compose down`.

### Ver que está funcionando

```
docker ps
```

Deberías ver dos filas con estado `Up ...`: `x-bot-tweets-1` y `x-telegram-poller-1`. Si alguna dice `Restarting` o no aparece, algo está fallando — mirá los logs (siguiente punto).

También podés confirmar que el servicio de tweets responde abriendo en el navegador:
```
http://localhost:5000/borradores/0
```
Un error `{"error": "no hay borrador para ese tweet_id"}` es la respuesta normal (quiere decir que el servicio está vivo y contestando).

### Ver los logs si algo no anda

```
docker compose logs -f
```

Muestra lo que van escribiendo los dos servicios en tiempo real (`Ctrl+C` para salir de la vista, sin que eso apague nada). Para ver solo uno de los dos:
```
docker compose logs -f bot-tweets
docker compose logs -f telegram-poller
```

### Archivos que NO hay que tocar sin saber lo que se hace

- **`.env`** — tiene las credenciales de la cuenta de X, el token del bot de Telegram y el id del chat. Si se edita mal (o se borra una línea), los bots dejan de poder conectarse y hay que volver a cargar todo a mano. Nunca lo compartas ni lo subas a ningún lado público.
- **`datos/estado.db`** (y los archivos `estado.db-wal` / `estado.db-shm` que aparecen al lado mientras el sistema corre) — es la base de datos donde vive el estado de cada borrador (qué tweet es, qué paso tiene, qué texto se eligió). Borrarla o editarla a mano puede dejar tarjetas de Telegram "vivas" sin su borrador correspondiente, o duplicar publicaciones. Si hace falta un respaldo, copiá el archivo con el sistema apagado (`docker compose stop` primero) o con `sqlite3.backup()` (no una copia cruda: el modo WAL deja parte de los datos en `estado.db-wal`, que una copia de archivo sola no incluye).
- **`accounts.db`** — es la sesión guardada de la cuenta de X que usa `twscrape` para no tener que volver a iniciar sesión cada vez. Con la publicación real, esta sesión ya no es solo de lectura: es la misma que usa `ClienteX.publicar_tweet` para publicar de verdad (ver sección 3). Borrarla obliga a cargar la cuenta de nuevo, y mientras tanto no se puede ni leer ni publicar.
- **`estado_db.py`** — no es un archivo de datos, es el código de acceso a `datos/estado.db` que comparten `bot_x.py` y `bot_telegram.py` (ver sección 1). Se copia a la imagen de Docker igual que los otros dos `.py` (ver `Dockerfile`), así que si lo movés o le cambiás el nombre hay que actualizar el `COPY` ahí también.
