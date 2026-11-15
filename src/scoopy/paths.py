import os
import sys

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LINKS_FILE = os.path.join(BASE_DIR, 'links.txt')
CHROME_PROFILE_DIR = os.path.join(BASE_DIR, '.chrome-profile')
ENCRYPTED_PROFILE_DIR = os.path.join(BASE_DIR, '.chrome-profile.enc')
PROFILE_UNLOCK_MARKER = os.path.join(BASE_DIR, '.scoopy-profile.unlocked')
