# Используем официальный образ с уже установленными браузерами Playwright
FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

WORKDIR /app

# Копируем все файлы в контейнер
COPY . /app

# Устанавливаем библиотеки Python
RUN pip install --no-cache-dir -r requirements.txt

# Запускаем бота
CMD ["python", "app.py"]
