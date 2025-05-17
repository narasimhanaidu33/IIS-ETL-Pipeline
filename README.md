End-to-End Data Engineering Project | Business Intelligence with IIS Logs

Designed and implemented a scalable ETL pipeline that transforms raw IIS web server logs into actionable insights, simulating a real-world business intelligence use case.

Key Highlights:

 Business Impact: Monitored security threats, optimized web performance, and analyzed user 
 behavior—essential for data-driven decisions.

 ETL Pipeline: Built in Apache Airflow using:
 • Custom multi-line log parsers
 • Geolocation enrichment with MaxMind DB
 • Automated data quality checks
 • Data Modeling: Star schema in PostgreSQL, enabling structured analytics.
 • Visualization: Connected to Metabase for dashboards on traffic trends, anomaly detection, and 
 device analytics.

 Design Decisions:
 • Privacy First: Complete PII redaction for GDPR compliance.
 • Cost vs. Accuracy: Chose offline GeoIP over APIs for efficiency and reliability.
 • Tooling: Picked Metabase for its open-source flexibility and SQL support.

 Skills Reinforced: Airflow orchestration, data modeling, indexing, and batch processing.

 Tools Used: Apache Airflow | PostgreSQL | Metabase | Python | MaxMind GeoIP

Grateful to my mentor, Mr. Craig, for the feedback and support!

#DataEngineering #ETL #Airflow #BusinessIntelligence #DataPipelines #PostgreSQL
