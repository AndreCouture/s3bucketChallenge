FROM python:3.13.0rc2-slim

COPY s3bucketstats.py /
RUN chmod +x /s3bucketstats.py
RUN pip install boto3 pandas requests botocore
ENTRYPOINT ["/s3bucketstats.py"]
CMD []

