FROM instantlinux/python-wsgi:latest

EXPOSE 8080
WORKDIR /opt/app
COPY Pipfile* uwsgi.ini /usr/src/
RUN apk add libxslt-dev libxml2-dev g++ && cd /usr/src && pip install -U pip && pipenv install --system --deploy && \
    mkdir /var/opt/app && chown uwsgi /var/opt/app

COPY main.py /opt/app
RUN chmod -R g-w,o-w /opt/app