FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY website_assistant ./website_assistant
COPY site_runtime ./site_runtime
COPY web ./web
EXPOSE 8001
CMD ["python", "-m", "site_runtime.api"]
