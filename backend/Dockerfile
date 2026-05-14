FROM python:3.12-slim

# Force UTF-8 throughout the Python runtime — fixes Unicode mojibake in Docker
ENV PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create a default .env from example if none exists
RUN test -f .env || cp .env.example .env

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
