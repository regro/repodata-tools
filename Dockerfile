FROM condaforge/miniforge3

# bust the docker cache so that we always rerun the installs below
ADD http://www.randomtext.me/api/gibberish /opt/docker/etc/gibberish

COPY . /opt/app
RUN cd /opt/app && \
    conda env create --file environment.yml && \
    /bin/bash -c "source activate test && pip install -e . " && \
    conda clean -tipsy && \
    find /opt/conda -follow -type f -name '*.a' -delete && \
    find /opt/conda -follow -type f -name '*.pyc' -delete && \
    conda clean -afy

CMD ["tini", "-s", "--", "/opt/app/run_app.sh"]
