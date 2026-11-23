FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Корневой сертификат Минцифры — нужен для подключения к GigaChat
RUN mkdir -p certs && python -c "import urllib.request as u; u.urlretrieve('https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer', 'certs/russian_trusted_root_ca.cer')" \
    || echo "WARNING: сертификат Минцифры не скачан"
COPY . .
CMD ["python", "-m", "newsbot.main"]
