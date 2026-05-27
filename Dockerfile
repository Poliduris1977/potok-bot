# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Зависимости отдельным слоем — кэшируются при rebuild если requirements не менялись
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Не запускать от root
RUN useradd -m potok && chown -R potok:potok /app
USER potok

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
