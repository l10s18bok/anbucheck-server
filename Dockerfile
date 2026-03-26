FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# PORT 환경변수를 Python에서 직접 읽어 uvicorn 실행
CMD ["python", "main.py"]
