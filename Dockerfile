FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY estado_db.py bot_x.py bot_telegram.py .

CMD ["python", "bot_x.py"]
