
raise RuntimeError("Private processing producers are not distributed and cannot be started.")

from app.services.stage4_celery import celery_app


__all__ = ["celery_app"]
