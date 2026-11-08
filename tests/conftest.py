import os

# The suite asserts the Linux/Xvfb behavior (tabs are brought to front, etc.). On a macOS
# developer machine the worker would otherwise switch to its hidden-Chrome mode; that mode
# has its own tests (test_macos_window.py), which patch it on explicitly.
os.environ.setdefault('PI_NODRIVER_MACOS_WINDOW', 'visible')
