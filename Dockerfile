FROM python:3.12.11-slim-bookworm AS web

ARG SHUNDA_RELEASE_VERSION
ARG SHUNDA_RELEASE_REVISION
ARG SHUNDA_RELEASE_CREATED

LABEL org.opencontainers.image.version=$SHUNDA_RELEASE_VERSION \
      org.opencontainers.image.revision=$SHUNDA_RELEASE_REVISION \
      org.opencontainers.image.created=$SHUNDA_RELEASE_CREATED

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHUNDA_RELEASE_VERSION=$SHUNDA_RELEASE_VERSION

WORKDIR /app

COPY pyproject.toml manage.py /app/
COPY config /app/config
COPY apps /app/apps
COPY scripts /app/scripts
COPY templates /app/templates
COPY static /app/static

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "." \
    && apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --show-error --silent \
       --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
       https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $VERSION_CODENAME-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends --yes postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system --gid 10001 app \
    && adduser --system --uid 10001 --ingroup app app \
    && chmod +x /app/scripts/*.sh \
    && chown -R app:app /app

USER app

RUN build_secret="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')" \
    && build_password="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
    && DJANGO_SETTINGS_MODULE=config.settings.prod \
       DJANGO_SECRET_KEY="$build_secret" \
       DJANGO_ALLOWED_HOSTS=build.invalid \
       CSRF_TRUSTED_ORIGINS=https://build.invalid \
       COMPANY_TAX_ID=91320281MA00000001 \
       DATABASE_URL="postgresql://build:$build_password@db:5432/build" \
       SHUNDA_RELEASE_VERSION="${SHUNDA_RELEASE_VERSION:-v0.0.0}" \
       SHUNDA_UPDATER_URL=http://updater:8090 \
       SHUNDA_UPDATER_TOKEN="$(python -c 'print("u" * 32)')" \
       python manage.py collectstatic --noinput

FROM docker:27.5.1-cli AS updater

RUN apk add --no-cache ca-certificates curl docker-cli-compose python3

COPY updater /app/updater

WORKDIR /app

ENTRYPOINT ["python3", "-m", "updater.main"]
