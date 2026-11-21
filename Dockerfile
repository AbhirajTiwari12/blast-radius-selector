# The scorer builds this on linux/amd64 and runs it with no network. The digest is the
# image the commits were labeled in; the selector runs sqlglot's own tests in it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /app/requirements.lock
# sqlglot's own repository at the pinned commit
RUN python -c "import urllib.request, tarfile, io, os; \
c='eb11aec4be8cb2122afe713a4816662c3f0fb2fa'; \
d=urllib.request.urlopen('https://github.com/tobymao/sqlglot/archive/'+c+'.tar.gz').read(); \
tarfile.open(fileobj=io.BytesIO(d)).extractall('/app', filter='data'); \
os.rename('/app/sqlglot-'+c, '/app/src')" \
 && python -m compileall -q /app/src/sqlglot /app/src/tests \
 && chmod -R a+rX /app
COPY selector.py /app/selector.py
COPY map /app/map
RUN chmod -R a+rX /app
ENV OPENBLAS_NUM_THREADS=1
CMD ["python", "/app/selector.py"]
