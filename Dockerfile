# Use an official Python runtime based on alpine as a parent image.
FROM python:3.12.14-alpine3.24

# Add user that will be used in the container.
RUN addgroup -g 1000 -S quant && adduser quant -h /home/quant -D -G quant  -u 1000 -s /bin/sh 

# Port used by this container to serve HTTP.
EXPOSE 8080

# Set environment variables.
# 1. Force Python stdout and stderr streams to be unbuffered.
# 2. Set PORT variable that is used by Gunicorn. This should match "EXPOSE"
#    command.
ENV PYTHONUNBUFFERED=1 \
    PORT=8000

# Install system packages required by quant and Django.
RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.aliyun.com/g' /etc/apk/repositories;\
apk update;\
apk upgrade;\
apk add --no-cache nginx nodejs npm libcap;


# Install the project requirements.
COPY requirements.txt /
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/;\
pip install --root-user-action=ignore -r /requirements.txt;\
pip install --root-user-action=ignore "gunicorn";

# Use /app folder as a directory where the source code is stored.
WORKDIR /home/quant/quant

# Set this directory to be owned by the "quant" user. This quant project
# uses SQLite, the folder needs to be owned by the user that
# will be writing to the database file.
RUN mkdir --parents /usr/local/nginx/logs/; \
rm -rf /var/lib/nginx/logs;\
mkdir --parents /var/lib/nginx/logs/;\
mkdir --parents /home/quant/quant/;\
chown -Rch quant:quant /usr/local/nginx/;\
chown -Rch quant:quant /home/quant/;\
chown -Rch quant:quant /var/lib/nginx/;\
setcap cap_net_bind_service=+ep /usr/sbin/nginx;
# Copy the source code of the project into the container.
COPY --chown=quant:quant . /home/quant/quant/

# Use user "quant" to run the build commands below and the server itself.
USER quant


RUN nginx -t -c /home/quant/quant/nginx/nginx.conf

# Collect static files.
RUN cd quant-frontend;\
npm install;\
npm run build;\
cd ..
# Runtime command that executes when "docker run" is called, it does the
# following:
#   1. Migrate the database.
#   2. Start the application server.
# WARNING:
#   Migrating database at the same time as starting the server IS NOT THE BEST
#   PRACTICE. The database should be migrated manually or using the release
#   phase facilities of your hosting platform. This is used only so the
#   quant instance can be started with a simple "docker run" command.
CMD set -xe; \
nginx -c /home/quant/quant/nginx/nginx.conf;\
python manage.py makemigrations --noinput; \
python manage.py migrate --noinput; \
gunicorn quant.wsgi:application
