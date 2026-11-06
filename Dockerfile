FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py auth_access.py registry.py registry_promotion.py juris_163.py juris_164.py ./
ENV PORT=8000
CMD ["sh", "-c", "python -c 'from auth_access import check_database_connection; print(check_database_connection())' && exec uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
