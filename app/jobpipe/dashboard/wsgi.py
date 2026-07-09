import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jobpipe.dashboard.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

# Ensure the SQLite schema exists even if the web container boots before the
# scheduler container (both mount the same data volume).
from jobpipe.db import connect  # noqa: E402

connect().close()

application = get_wsgi_application()
