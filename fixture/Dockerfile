# Request Tracker for the tool's fixture: one exact RT release, built from source.
#
# There is no official image of RT itself. Best Practical publishes images with
# RT's dependencies (bpssysadmin/rt-base-debian), which RT's own repository uses
# to run its tests; each release's Dockerfile names the one it was tested on.
# This file starts from that image, pinned by digest, and installs the release
# from RT's git repository, pinned by the commit its tag points at: the build
# fails if the tag ever points anywhere else.
#
# Built by docker-compose.fixture.yml with the arguments for one RT version (see
# the Makefile). For testing only: it runs no web server and sends no mail.

ARG RT_BASE_IMAGE
FROM ${RT_BASE_IMAGE}

ARG RT_VERSION
ARG RT_COMMIT

RUN git clone --quiet --depth 1 --branch "rt-${RT_VERSION}" https://github.com/bestpractical/rt.git /usr/src/rt \
    && test "$(git -C /usr/src/rt rev-parse HEAD)" = "${RT_COMMIT}"

WORKDIR /usr/src/rt
RUN autoconf \
    && ./configure --prefix=/opt/rt --with-db-type=Pg \
        --with-web-user=root --with-web-group=root --with-rt-group=root \
        --with-bin-owner=root --with-libs-owner=root --with-libs-group=root \
        --with-web-handler=standalone \
    && make install \
    && rm -rf /usr/src/rt

COPY RT_SiteConfig.pm /opt/rt/etc/RT_SiteConfig.pm
COPY entrypoint.pl /usr/local/bin/rt-fixture-entrypoint

ENV PERL5LIB=/opt/rt/lib
ENTRYPOINT ["perl", "/usr/local/bin/rt-fixture-entrypoint"]
