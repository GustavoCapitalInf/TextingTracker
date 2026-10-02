"""Light, dark or device-matched appearance, remembered per browser in a cookie.

The server reads the cookie and renders <html data-theme="..."> so the first paint is
already in the chosen theme; no script runs before the page and nothing else is stored.
"""
THEME_COOKIE = 'phonetracker_theme'
THEME_COOKIE_AGE = 365 * 24 * 60 * 60
DEFAULT_THEME = 'dark'
THEMES = {'dark': 'Dark', 'light': 'Light', 'system': 'Auto'}
COLOR_SCHEMES = {'dark': 'dark', 'light': 'light', 'system': 'dark light'}


def current_theme(request):
    theme = request.COOKIES.get(THEME_COOKIE, '') if request is not None else ''
    return theme if theme in THEMES else DEFAULT_THEME
