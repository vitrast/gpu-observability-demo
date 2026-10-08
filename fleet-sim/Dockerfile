FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.2
COPY sim.py topology.yaml ./
EXPOSE 9100
CMD ["python", "sim.py"]
