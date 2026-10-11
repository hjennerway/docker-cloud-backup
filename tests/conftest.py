import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="cloud-backup-test-app-")
os.environ["ADMIN_PASSWORD"] = "test-password-only"
