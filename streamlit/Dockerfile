FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py api_client.py ui_helpers.py styles.css ./
COPY assets ./assets
COPY .streamlit ./.streamlit
COPY tests ./tests

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
