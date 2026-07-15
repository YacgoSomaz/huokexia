"""Build-time public licensing settings.

The source tree deliberately stays unlocked. `build_release.ps1 -Commercial`
replaces this file in the staging copy with a non-secret public key, HTTPS
license-server URL, and enforcement flag.
"""

LICENSE_ENFORCE = False
LICENSE_SERVER_URL = ""
LICENSE_PUBLIC_KEY = ""
LICENSE_APP_VERSION = ""
# The commercial staging step writes the fixed product code here.  It is empty
# in the shared development tree so each standalone product can select its own
# non-production identity without weakening a commercial package.
LICENSE_PRODUCT_CODE = ""

# Commercial account builds replace these public values during packaging.  The
# desktop app only ever ships the account-service URL and Ed25519 public key;
# SMS, payment and signing private keys never leave the server.
ACCOUNT_API_URL = ""
ACCOUNT_PUBLIC_KEY = ""
ACCOUNT_PRODUCT_CODE = "replay_shrimp"
# update-v1 is deliberately a different public key from account-v1.  It signs
# installer metadata only; it can never unlock a product entitlement.
UPDATE_PUBLIC_KEY = ""
