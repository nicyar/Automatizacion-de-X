"""Script de QA aislado para confirmar en el chat real de Telegram el arreglo del doble
"Anotado" (ver bot_telegram.py::sacar_boton). Manda/borra mensajes de prueba directo contra la
API HTTP de Telegram con urllib de la stdlib -- no importa bot_x.py ni bot_telegram.py, no toca
estado.db ni ningún workflow de n8n, y no agrega dependencias nuevas.

El botón que manda usa callback_data="tw:prueba-<random>", mismo formato tw:<id> que usan los
avisos reales, para que bot_telegram.py (el poller) lo trate como un aviso de tweet cualquiera.

Uso:
    python tools/prueba_qa_boton.py            # manda un mensaje de prueba, imprime message_id
    python tools/prueba_qa_boton.py --borrar 123  # borra el mensaje de prueba 123
"""
import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENV_PATH = RAIZ / '.env'

TEXTO_PRUEBA = (
    "[PRUEBA QA — ignorar, no es un tweet real]\n\n"
    "Mensaje de prueba para chequear a mano el arreglo del doble \"Anotado\"."
)
TEXTO_BOTON = "🐦 Twittear algo parecido"


def leer_env(clave):
    """Lee una clave de .env a mano (python-dotenv no está en requirements.txt)."""
    if not ENV_PATH.exists():
        sys.exit(f"No se encontró {ENV_PATH}")
    prefijo = f'{clave}='
    for linea in ENV_PATH.read_text(encoding='utf-8').splitlines():
        linea = linea.strip()
        if linea.startswith(prefijo):
            valor = linea.split('=', 1)[1].strip()
            if not valor:
                sys.exit(f"{clave} está vacío en .env")
            return valor
    sys.exit(f"{clave} no está en .env")


def llamar(token, metodo, payload):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # Telegram manda el detalle del error en el body, no en la excepción.
        return json.load(e)


def enviar():
    token = leer_env('TELEGRAM_BOT_TOKEN')
    callback_data = f"tw:prueba-{secrets.token_hex(4)}"
    payload = {
        'chat_id': int(leer_env('CHAT_ID')),
        'text': TEXTO_PRUEBA,
        'reply_markup': {
            'inline_keyboard': [[{'text': TEXTO_BOTON, 'callback_data': callback_data}]],
        },
    }
    resultado = llamar(token, 'sendMessage', payload)
    if not resultado.get('ok'):
        sys.exit(f"Telegram devolvió error al mandar: {resultado}")
    message_id = resultado['result']['message_id']
    print(f"ok=True message_id={message_id} callback_data={callback_data}")


def borrar(message_id):
    token = leer_env('TELEGRAM_BOT_TOKEN')
    resultado = llamar(token, 'deleteMessage', {'chat_id': int(leer_env('CHAT_ID')), 'message_id': message_id})
    if not resultado.get('ok'):
        sys.exit(f"Telegram devolvió error al borrar: {resultado}")
    print(f"ok=True message_id={message_id} borrado")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--borrar', type=int, metavar='MESSAGE_ID', help='Borra el mensaje de prueba con ese message_id en vez de mandar uno nuevo.')
    args = parser.parse_args()

    if args.borrar is not None:
        borrar(args.borrar)
    else:
        enviar()


if __name__ == '__main__':
    main()
