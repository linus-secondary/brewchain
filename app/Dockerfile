# One image for all four services (SERVICE=menu|orders|loyalty|gateway)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV SERVICE=gateway \
    PYTHONUNBUFFERED=1
EXPOSE 8000

# Run as a normal user, not root
RUN useradd --create-home brew
USER brew

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
