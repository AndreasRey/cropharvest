# https://github.com/perrygeo/docker-gdal-base
FROM perrygeo/gdal-base:latest

RUN mkdir /app/

COPY /app/  /app/

RUN pip3 install -r ../app/REQUIREMENTS.txt

# Keeps the container running
CMD ["tail", "-f", "/dev/null"]