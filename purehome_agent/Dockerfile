ARG BUILD_FROM
FROM ${BUILD_FROM}

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY agent /opt/purehome/agent
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /opt/purehome
CMD [ "/run.sh" ]
