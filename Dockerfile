FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md requirements-tested.txt /app/
COPY prismagenticpay /app/prismagenticpay
RUN pip install --no-cache-dir -r requirements-tested.txt && pip install --no-cache-dir --no-deps -e .
EXPOSE 8080
CMD ["uvicorn", "prismagenticpay.runtime:create_production_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
