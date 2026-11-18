"""Regression harness for retained legacy widget helpers; not a production route."""
from core import jobs
from ui import app
# Like the original script entry, bind patched queue tools on every test run.
app.submit_job = jobs.submit_job
app.ensure_worker = jobs.ensure_worker
app.legacy_main()
