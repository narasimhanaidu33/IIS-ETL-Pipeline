FROM apache/airflow:2.6.2

USER root
# Install system dependencies if needed (e.g., for geoip)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

USER airflow
# Install Python packages
RUN pip install --user \
    user_agents==2.2.0 \
    pandas==1.3.5 \
    sqlalchemy==1.4.46 \
    ipinfo==4.4.0 \
    geoip2==4.6.0

# Ensure packages are in Python path
ENV PYTHONPATH=/home/airflow/.local/lib/python3.7/site-packages:$PYTHONPATH
