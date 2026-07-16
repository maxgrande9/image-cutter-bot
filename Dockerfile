FROM python:3.11-slim

WORKDIR /app

# Минимум системных библиотек для Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev \
    zlib1g-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Обновляем pip
RUN pip install --upgrade pip setuptools wheel

# Устанавливаем Python-зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Для корректного вывода логов
ENV PYTHONUNBUFFERED=1

# Запуск
CMD ["python", "main.py"]