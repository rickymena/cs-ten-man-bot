# Process exit codes. Monitoring can alert on these numbers.
EXIT_OK = 0
EXIT_DB_FAILED = 3     # database or backup failure (utils/backup.py)
EXIT_NOTIFY_FAILED = 4  # Discord delivery failed or webhook misconfigured (devtool.py)
EXIT_CONFIG = 7        # bad .env / missing settings / bad CLI usage: refuses to start
